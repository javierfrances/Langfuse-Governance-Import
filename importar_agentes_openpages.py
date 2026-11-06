#!/usr/bin/env python3
"""Importa SOLO agentes del inventario Langfuse a OpenPages SaaS en IBM Cloud.

Por defecto, vista previa local sin llamadas HTTP.
Autenticacion: OP_API_KEY -> IBM Cloud IAM access token -> Bearer OpenPages REST V2.
Sin dependencias de terceros. Sin cambios en AI Discovery.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

OBJECT_TYPE = "AIComponent"
MARKER = "Langfuse inventory ID: "
RESULT_FILE = "resultado_importacion_agentes.json"
IAM_DEFAULT = "https://iam.cloud.ibm.com/identity/token"

class APIError(RuntimeError):
    pass


def validate_https(value, label):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.netloc or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError(f"{label} debe ser una URL HTTPS sin usuario, contrasena, query ni fragmento")
    return value


def api_url():
    explicit = os.getenv('OP_API_BASE_URL', '').strip().rstrip('/')
    if explicit:
        return validate_https(explicit, 'OP_API_BASE_URL')
    host = os.getenv('OP_BASE_URL', '').strip().rstrip('/')
    if not host:
        raise ValueError('Falta OP_BASE_URL (Fixed URL del servicio OpenPages) u OP_API_BASE_URL')
    validate_https(host, 'OP_BASE_URL')
    if host.endswith('/opgrc/api/v2'):
        return host
    return host + '/opgrc/api/v2'


def http_json(url, *, method='GET', payload=None, headers=None, form=None, timeout=30):
    hdrs = {'Accept': 'application/json', 'User-Agent': 'langfuse-openpages-agent-import/2.0'}
    hdrs.update(headers or {})
    raw = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        hdrs['Content-Type'] = 'application/json'
    elif form is not None:
        raw = urllib.parse.urlencode(form).encode('utf-8')
        hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    request = urllib.request.Request(url, headers=hdrs, data=raw, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode('utf-8', errors='replace')
            try:
                result = json.loads(content) if content.strip() else {}
            except ValueError as exc:
                raise APIError(f'Respuesta no JSON de {method} {urlsplit(url).path} (HTTP {response.status})') from exc
            return response.status, result
    except urllib.error.HTTPError as exc:
        content = exc.read().decode('utf-8', errors='replace')
        # No imprimir el cuerpo de peticiones a IAM, por seguridad.
        if urlsplit(url).netloc == urlsplit(IAM_DEFAULT).netloc:
            content = '[contenido omitido por seguridad]'
        raise APIError(f'HTTP {exc.code} en {method} {urlsplit(url).path}: {content[:800]}') from exc
    except urllib.error.URLError as exc:
        raise APIError(f'Error de red en {method} {urlsplit(url).path}: {exc.reason}') from exc


class OpenPages:
    def __init__(self):
        self.base = api_url()
        key = os.getenv('OP_API_KEY', '').strip()
        if not key:
            raise ValueError('Falta OP_API_KEY: usa la API key de IBM Cloud IAM de la cuenta con acceso a OpenPages SaaS')
        iam_url = validate_https(os.getenv('OP_IAM_URL', IAM_DEFAULT).strip(), 'OP_IAM_URL')
        _, data = http_json(iam_url, method='POST', form={
            'grant_type': 'urn:ibm:params:oauth:grant-type:apikey',
            'apikey': key,
        })
        token = data.get('access_token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise APIError('IAM no devolvio access_token. Comprueba la API key y la cuenta.')
        self.auth_header = {'Authorization': 'Bearer ' + token}

    def request(self, method, path, payload=None):
        return http_json(self.base + path, method=method, payload=payload, headers=self.auth_header)

    def type_id(self):
        _, data = self.request('GET', '/types/' + OBJECT_TYPE)
        typ = data.get('id') if isinstance(data, dict) else None
        if typ is None or not str(typ).isdigit():
            raise APIError(f'/types/{OBJECT_TYPE} no devolvio un ID numerico: {str(data)[:350]}')
        return str(typ)

    def query(self, statement, offset=0, limit=200):
        status, data = self.request('POST', '/query', {
            'statement': statement, 'offset': offset, 'limit': limit,
            'case_insensitive': False, 'max_rows': 0,
        })
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get('rows'), list):
            raise APIError('Respuesta inesperada de /query; no se puede comprobar duplicados')
        rows = []
        for row in data['rows']:
            fields = row.get('fields', [])
            if isinstance(fields, dict):
                fields = fields.get('field', [])
            if not isinstance(fields, list):
                raise APIError('Formato inesperado de rows.fields. Abortando')
            rows.append({str(f.get('name', '')).casefold(): f.get('value')
                         for f in fields if isinstance(f, dict)})
        return rows

    def query_all(self, statement):
        total = []
        for offset in range(0, 100000, 200):
            batch = self.query(statement, offset, 200)
            total.extend(batch)
            if len(batch) < 200:
                return total
        raise APIError('Demasiadas paginas en consulta; abortando')

    def existing_agents(self):
        return self.query_all('SELECT [Resource ID], [Name], [Description] FROM [AIComponent]')

    def business_entities(self):
        return self.query_all('SELECT [Resource ID], [Name] FROM [SOXBusEntity]')

    def parent_allows_agent(self, parent_id):
        # Seguridad: verificar el parent y permiso antes de hacer POST.
        status, response = self.request('GET',
            f'/contents/{urllib.parse.quote(str(parent_id), safe="")}/permissions/cancreate/{OBJECT_TYPE}')
        value = response.get('response') if isinstance(response, dict) else None
        if status != 200 or str(value).lower() != 'true':
            raise APIError(f'No hay permiso de crear {OBJECT_TYPE} bajo el objeto padre {parent_id}: {response}')

    def create_agent(self, name, description, parent_id, type_definition_id):
        payload = {
            'name': name,
            'description': description,
            'type_definition_id': type_definition_id,
            'primary_parent_id': str(parent_id),
            'fields': [],
        }
        status, result = self.request('POST', '/contents', payload)
        if status != 201 or not isinstance(result, dict) or not result.get('id'):
            raise APIError(f'Creacion sin respuesta 201 + id: HTTP {status}, {str(result)[:400]}')
        return str(result['id'])


def read_agents(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    items = data.get('inventory', {}).get('agents', [])
    if not isinstance(items, list) or not items:
        raise ValueError('El fichero no contiene inventory.agents como lista no vacia')
    seen = set()
    for x in items:
        if not isinstance(x, dict) or not isinstance(x.get('id'), str) or not isinstance(x.get('name'), str):
            raise ValueError('Agente invalido en inventario (falta id o name)')
        if not x['id'].strip() or not x['name'].strip() or x['id'] in seen:
            raise ValueError('ID/nombre vacio o ID duplicado en inventario')
        seen.add(x['id'])
    return items, data.get('source', {})


def agent_description(agent, source):
    parts = [
        '[Importacion de inventario de observabilidad: Langfuse]',
        MARKER + agent['id'],
        'Langfuse project ID: ' + str(source.get('project_id', 'desconocido')),
        'Registro de gobierno creado a partir de trazas; no ejecuta el agente.',
    ]
    identity = agent.get('identity') or {}
    if isinstance(identity, dict) and identity.get('external_id'):
        parts.append('External agent ID: ' + str(identity['external_id']))
    evidence = agent.get('first_seen') or {}
    if isinstance(evidence, dict) and evidence.get('trace_id'):
        parts.append('Evidence trace ID: ' + str(evidence['trace_id']))
    return '\n'.join(parts)


def existing_indexes(rows):
    by_id, by_name = {}, {}
    for row in rows:
        oid = str(row.get('resource id') or '').strip()
        name = str(row.get('name') or '').strip()
        desc = str(row.get('description') or '')
        for line in desc.splitlines():
            if line.startswith(MARKER):
                by_id[line[len(MARKER):].strip()] = (oid, name)
        if name:
            by_name.setdefault(name.casefold(), []).append(oid)
    return by_id, by_name


def save_report(path, results, input_path):
    target = Path(path)
    target.parent.mkdir(exist_ok=True, parents=True)
    doc = {'source': 'Langfuse', 'object_type': OBJECT_TYPE,
           'input_file': input_path, 'results': results}
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8',
                                     dir=target.parent, prefix='.op_result_', suffix='.json') as out:
        json.dump(doc, out, indent=2, ensure_ascii=False)
        out.write('\n')
        tmp = out.name
    os.replace(tmp, target)


def main(argv=None):
    p = argparse.ArgumentParser(description='Langfuse JSON -> Agent (AIComponent) OpenPages SaaS IBM Cloud')
    p.add_argument('--input', default='inventario_langfuse.json')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--test-connection', action='store_true', help='Valida IAM y GET tipo Agent, sin crear')
    modes.add_argument('--list-business-entities', action='store_true')
    modes.add_argument('--check', action='store_true', help='Consulta duplicados sin crear')
    modes.add_argument('--apply', action='store_true', help='Crea objetos nuevos')
    p.add_argument('--parent-id', default=os.getenv('OP_PRIMARY_PARENT_ID', ''))
    p.add_argument('--max-create', type=int, default=0, help='0 = sin limite')
    p.add_argument('--result', default=RESULT_FILE)
    args = p.parse_args(argv)
    if args.max_create < 0:
        p.error('--max-create debe ser >= 0')
    if args.test_connection:
        api = OpenPages()
        type_id = api.type_id()
        print(f'Conexion IAM/OpenPages correcta. {OBJECT_TYPE} type_definition_id={type_id}')
        return 0
    if args.list_business_entities:
        api = OpenPages()
        for row in api.business_entities():
            print(f"{row.get('resource id', '?'):>12}  {row.get('name', '?')}")
        return 0

    agents, source = read_agents(args.input)
    print(f'Inventario: {args.input} | Agentes: {len(agents)} | Solo tipo: {OBJECT_TYPE}')
    for agent in agents:
        print(f"  - {agent['name']} ({agent['id']})")
    if not (args.check or args.apply):
        print('\nVista previa LOCAL. Ninguna peticion de red ni creacion.')
        return 0
    if args.apply and not args.parent_id:
        p.error('Para --apply necesitas --parent-id con el Resource ID de un padre valido')

    api = OpenPages()
    type_id = api.type_id()
    existing = api.existing_agents()  # Nunca crear si no se pueden leer los agentes existentes.
    print(f'Conectado. Tipo {OBJECT_TYPE} ID={type_id}. Agentes ya visibles: {len(existing)}')
    ids, names = existing_indexes(existing)
    if args.apply:
        api.parent_allows_agent(args.parent_id)
    created = 0
    results = []
    for agent in agents:
        name, key = agent['name'].strip(), agent['id'].strip()
        if key in ids:
            result, detail = 'already_exists', f'Coincidencia ID Langfuse; OpenPages ID {ids[key][0]}'
        elif name.casefold() in names:
            result, detail = 'name_conflict', f'Nombre ya existe (objetos {names[name.casefold()]}); omitido por seguridad'
        elif not args.apply:
            result, detail = 'would_create', 'No se encuentra en la consola'
        elif args.max_create and created >= args.max_create:
            result, detail = 'deferred', 'Limite de prueba alcanzado'
        else:
            try:
                new_id = api.create_agent(name, agent_description(agent, source), args.parent_id, type_id)
            except APIError as exc:
                results.append({'langfuse_id': key, 'name': name, 'status': 'error', 'detail': str(exc)})
                save_report(args.result, results, args.input)
                raise
            created += 1
            ids[key] = (new_id, name)
            names[name.casefold()] = [new_id]
            result, detail = 'created', 'OpenPages Resource ID ' + new_id
        print(f'[{result}] {name}: {detail}')
        results.append({'langfuse_id': key, 'name': name, 'status': result, 'detail': detail})
        save_report(args.result, results, args.input)
    print(f'Finalizado: creados {created}. Informe: {args.result}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, APIError, OSError, json.JSONDecodeError) as err:
        print('ERROR:', err, file=sys.stderr)
        raise SystemExit(1)

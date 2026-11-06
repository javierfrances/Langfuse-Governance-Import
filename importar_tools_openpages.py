#!/usr/bin/env python3
"""Importa AI Tools del inventario Langfuse bajo un Agent ya creado en OpenPages SaaS.

No ejecuta agentes ni tools. Solo registra recursos de gobierno mediante API V2.
Autenticacion IBM Cloud IAM desde OP_API_KEY (modulo importar_agentes_openpages.py).
Por defecto no hace llamadas de red; --check consulta; --apply crea como maximo 1.
"""
import argparse
import json
import os
import tempfile
from pathlib import Path
import sys
from urllib.parse import quote

from importar_agentes_openpages import (
    APIError, MARKER as AGENT_MARKER, OpenPages,
)

TOOL_TYPE = 'AITool'
TOOL_MARKER = 'Langfuse tool inventory ID: '
PARENT_MARKER = 'Parent Langfuse agent ID: '
RESULT_FILE = 'resultado_importacion_tools.json'


def save_report(path, results, input_path):
    target = Path(path)
    target.parent.mkdir(exist_ok=True, parents=True)
    doc = {'source': 'Langfuse', 'object_type': TOOL_TYPE,
           'input_file': input_path, 'results': results}
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8',
                                     dir=target.parent, prefix='.op_tools_', suffix='.json') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write('\n')
        temp = f.name
    os.replace(temp, target)


def inventory(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    inv = raw.get('inventory')
    if not isinstance(inv, dict):
        raise ValueError('JSON sin campo inventory')
    agents = inv.get('agents', [])
    tools = inv.get('tools', [])
    if not isinstance(agents, list) or not isinstance(tools, list):
        raise ValueError('inventory.agents y inventory.tools deben ser listas')
    for category, items in (('agents', agents), ('tools', tools)):
        ids = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('name'), str):
                raise ValueError(f'{category}: elemento sin id/name')
            if not item['id'].strip() or not item['name'].strip() or item['id'] in ids:
                raise ValueError(f'{category}: ID vacio o duplicado')
            ids.add(item['id'])
    return agents, tools, raw.get('source') or {}


def select_agent(agents, label):
    matches = [a for a in agents if a['id'] == label or a['name'].casefold() == label.casefold()]
    if len(matches) != 1:
        raise ValueError(f'No se encuentra exactamente un agente {label!r} en inventory.agents')
    return matches[0]


def tools_of_agent(agent, tools):
    aid = agent['id']
    linked = set((agent.get('relationships') or {}).get('tool_ids') or [])
    result = []
    for t in tools:
        parent_ids = set((t.get('relationships') or {}).get('agent_ids') or [])
        if t['id'] in linked or aid in parent_ids:
            result.append(t)
    return result


def get_unique_agent_in_op(api, agent):
    # Marcador escrito por el importador de agentes anterior. NO confiar solo en el nombre.
    rows = api.existing_agents()
    matches = []
    names = []
    for row in rows:
        name = str(row.get('name') or '')
        desc = str(row.get('description') or '')
        oid = str(row.get('resource id') or '')
        if name.casefold() == agent['name'].casefold():
            names.append(oid)
        if any(line.strip() == AGENT_MARKER + agent['id'] for line in desc.splitlines()):
            matches.append((oid, name))
    if len(matches) == 1 and matches[0][0]:
        return matches[0][0], matches[0][1]
    if not matches:
        raise APIError(
            f'Agente {agent["name"]!r} no localizado mediante marcador de inventario {agent["id"]!r} en OpenPages. '
            f'Coincidencias solo por nombre: {names}. Por seguridad no se asociaran tools por nombre.')
    raise APIError(f'Hay {len(matches)} agentes en OpenPages con el mismo marcador Langfuse. Abortando.')


def type_id(api, type_name=TOOL_TYPE):
    _, data = api.request('GET', '/types/' + type_name)
    oid = data.get('id') if isinstance(data, dict) else None
    if oid is None or not str(oid).isdigit():
        raise APIError(f'/types/{type_name} no devolvio id numerico: {str(data)[:300]}')
    return str(oid)


def existing_tools(api):
    return api.query_all('SELECT [Resource ID], [Name], [Description] FROM [AITool]')


def check_tool_create_permission(api, parent_id):
    _, data = api.request('GET', f'/contents/{quote(str(parent_id), safe="")}/permissions/cancreate/{TOOL_TYPE}')
    allowed = data.get('response') if isinstance(data, dict) else None
    if str(allowed).lower() != 'true':
        raise APIError(f'La instancia no permite crear {TOOL_TYPE} bajo Agent {parent_id}: {data}')


def tool_description(tool, agent, source):
    evidence = tool.get('first_seen') or {}
    identity = tool.get('identity') or {}
    parts = [
        '[Herramienta descubierta en trazas de Langfuse; no es ejecutable desde OpenPages]',
        TOOL_MARKER + tool['id'],
        PARENT_MARKER + agent['id'],
        'Source: Langfuse',
        'Langfuse project ID: ' + str(source.get('project_id', 'unknown')),
    ]
    if isinstance(identity, dict) and identity.get('external_id'):
        parts.append('External tool ID: ' + str(identity['external_id']))
    if isinstance(evidence, dict) and evidence.get('trace_id'):
        parts.append('Evidence trace ID: ' + str(evidence['trace_id']))
    return '\n'.join(parts)


def existing_tool_indexes(rows):
    by_marker, by_name = {}, {}
    for row in rows:
        oid = str(row.get('resource id') or '').strip()
        name = str(row.get('name') or '').strip()
        desc = str(row.get('description') or '')
        for line in desc.splitlines():
            if line.startswith(TOOL_MARKER):
                by_marker.setdefault(line[len(TOOL_MARKER):].strip(), []).append((oid, name))
        if name:
            by_name.setdefault(name.casefold(), []).append(oid)
    return by_marker, by_name


def create_tool(api, tool, agent, source, parent_id, tool_type_id):
    payload = {
        'name': tool['name'].strip(),
        'description': tool_description(tool, agent, source),
        'type_definition_id': tool_type_id,
        'primary_parent_id': str(parent_id),
        'fields': [],
    }
    status, result = api.request('POST', '/contents', payload)
    if status != 201 or not isinstance(result, dict) or not result.get('id'):
        raise APIError(f'Creacion sin HTTP 201 e id: HTTP {status}: {str(result)[:400]}')
    return str(result['id'])


def check_parent_association(api, tool_id, parent_id):
    # Solo lectura: si falla en esta instancia, se avisa pero NO se repite la creacion.
    _, data = api.request('GET', f'/contents/{quote(tool_id, safe="")}/associations/parents')
    if isinstance(data, list):
        assoc = data
    elif isinstance(data, dict):
        assoc = data.get('associations', data.get('items', []))
    else:
        assoc = []
    return any(str(x.get('id')) == str(parent_id) for x in assoc if isinstance(x, dict))


def main(argv=None):
    p = argparse.ArgumentParser(description='Langfuse inventory -> AITool asociadas a un AIComponent en OpenPages SaaS')
    p.add_argument('--input', default='inventario_langfuse.json')
    p.add_argument('--agent', required=True, help='Nombre exacto o ID de agente Langfuse dentro del JSON')
    actions = p.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true', help='Consulta la consola, SIN escribir')
    actions.add_argument('--apply', action='store_true', help='Crea tools, por defecto solo 1')
    p.add_argument('--max-create', type=int, default=1, help='Maximo de tools a crear (default: 1; 0: todas)')
    p.add_argument('--result', default=RESULT_FILE)
    args = p.parse_args(argv)
    if args.max_create < 0:
        p.error('--max-create debe ser >= 0')

    agents, tools, source = inventory(args.input)
    agent = select_agent(agents, args.agent)
    related = tools_of_agent(agent, tools)
    print(f'Agente seleccionado: {agent["name"]} ({agent["id"]})')
    print(f'Tools relacionadas en JSON: {len(related)}')
    for tool in related:
        print(f'  - {tool["name"]} [{tool["id"]}]')
    if not related:
        print('Nada que importar.')
        return 0
    if not (args.check or args.apply):
        print('Vista previa LOCAL. No se ha conectado a OpenPages ni creado objetos.')
        return 0

    api = OpenPages()
    parent_id, parent_name = get_unique_agent_in_op(api, agent)
    print(f'Agente OpenPages: {parent_name} | Resource ID: {parent_id}')
    tid = type_id(api)
    rows = existing_tools(api)
    by_marker, by_name = existing_tool_indexes(rows)
    print(f'Tipo {TOOL_TYPE} ID={tid} | Tools visibles en OpenPages: {len(rows)}')
    if args.apply:
        check_tool_create_permission(api, parent_id)
    results, created = [], 0
    for tool in related:
        marker = tool['id']
        name = tool['name'].strip()
        if marker in by_marker:
            ids = [x[0] for x in by_marker[marker]]
            state, detail = 'already_exists', f'Marcador Langfuse existente en OpenPages: {ids}. No se cambian asociaciones.'
        elif name.casefold() in by_name:
            ids = by_name[name.casefold()]
            state, detail = 'name_conflict', f'Existe una Tool con este nombre {ids}, pero sin marcador coincidente. No se duplica ni se reasocia automaticamente.'
        elif not args.apply:
            state, detail = 'would_create', f'Se crearia bajo Agent OpenPages {parent_id}'
        elif args.max_create and created >= args.max_create:
            state, detail = 'deferred', 'Limite de prueba alcanzado'
        else:
            try:
                new_id = create_tool(api, tool, agent, source, parent_id, tid)
            except APIError as exc:
                results.append({'tool_id': marker, 'name': name, 'status': 'error', 'detail': str(exc)})
                save_report(args.result, results, args.input)
                raise
            created += 1
            by_marker[marker] = [(new_id, name)]
            by_name.setdefault(name.casefold(), []).append(new_id)
            state, detail = 'created', f'AITool Resource ID {new_id}; parent Agent Resource ID {parent_id}'
            try:
                if check_parent_association(api, new_id, parent_id):
                    detail += ' | asociacion verificada'
                else:
                    detail += ' | ADVERTENCIA: asociacion no confirmada por GET; revisa interfaz'
            except APIError as exc:
                detail += f' | no se pudo comprobar asociacion: {exc}'
        results.append({'langfuse_tool_id': marker, 'name': name, 'parent_agent_langfuse_id': agent['id'],
                        'parent_agent_openpages_id': parent_id, 'status': state, 'detail': detail})
        print(f'[{state}] {name}: {detail}')
        if args.apply:
            save_report(args.result, results, args.input)
    if args.check:
        print('Solo consulta. No se ha creado ningun objeto.')
    else:
        print(f'Finalizado: {created} herramientas creadas. Reporte: {args.result}')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, APIError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        raise SystemExit(1)

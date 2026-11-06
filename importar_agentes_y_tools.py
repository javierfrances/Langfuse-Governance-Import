#!/usr/bin/env python3
"""Importa Agents, AI Tools y Models desde un JSON de Langfuse.

OpenPages SaaS: OP_API_KEY -> IBM IAM -> OpenPages REST API V2.
Modo por defecto: preview local; --check consulta consola; --apply crea.
--max-agents 1 importa el primer agente y todas sus tools relacionadas.
Marcadores de inventario en la descripcion permiten reintentos idempotentes.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote

from importar_agentes_openpages import (
    APIError, OpenPages, MARKER as AGENT_MARKER,
    agent_description, existing_indexes,
)
from importar_tools_openpages import (
    TOOL_MARKER, PARENT_MARKER,
    inventory, tools_of_agent, type_id, existing_tool_indexes,
    tool_description,
)
from importar_modelos_openpages import (
    MODEL_MARKER, models_from_json, models_of_agent, model_description, create_model,
)


def save_json_safely(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                     prefix='.import_', suffix='.tmp', delete=False) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
        temp = handle.name
    os.replace(temp, path)


def marker_index(rows, marker):
    """Mapeo marcador -> lista de (resource_id, nombre); detecta duplicados anómalos."""
    result = {}
    for row in rows:
        oid = str(row.get('resource id') or '').strip()
        name = str(row.get('name') or '').strip()
        desc = str(row.get('description') or '')
        for line in desc.splitlines():
            line = line.strip()
            if line.startswith(marker):
                key = line[len(marker):].strip()
                if key:
                    result.setdefault(key, []).append((oid, name))
    return result


def resolved_resource(rows, marker, inventory_id, name):
    by_id = marker_index(rows, marker)
    matches = by_id.get(inventory_id, [])
    if len(matches) > 1:
        raise APIError(f'{name}: hay {len(matches)} objetos con el mismo marcador {inventory_id}. Revisar manualmente.')
    if matches:
        if not matches[0][0]:
            raise APIError(f'{name}: registro encontrado sin Resource ID')
        return ('existing', matches[0][0])
    for row in rows:
        if str(row.get('name') or '').strip().casefold() == name.casefold():
            return ('name_conflict', str(row.get('resource id') or ''))
    return ('new', None)


def list_parent_ids(api, object_id):
    """Endpoint oficial: /contents/{id}/associations?association_type=parent."""
    _, doc = api.request('GET', f'/contents/{quote(str(object_id), safe="")}/associations?association_type=parent')
    if isinstance(doc, dict):
        assocs = doc.get('associations', [])
    elif isinstance(doc, list):
        assocs = doc
    else:
        raise APIError(f'Formato inesperado de asociaciones para {object_id}')
    if not isinstance(assocs, list):
        raise APIError(f'Listado de asociaciones no reconocido para {object_id}')
    return {str(it['id']) for it in assocs if isinstance(it, dict) and it.get('id') is not None}


def has_create_permission(api, parent_id, type_name):
    _, doc = api.request('GET',
        f'/contents/{quote(str(parent_id), safe="")}/permissions/cancreate/{type_name}')
    return str(doc.get('response', '')).lower() == 'true' if isinstance(doc, dict) else False


def create_tool(api, tool, agent, source, parent_id, tid):
    payload = {
        'name': tool['name'].strip(),
        'description': tool_description(tool, agent, source),
        'type_definition_id': tid,
        'primary_parent_id': str(parent_id),
        'fields': [],
    }
    status, doc = api.request('POST', '/contents', payload)
    if status != 201 or not isinstance(doc, dict) or not doc.get('id'):
        raise APIError(f'No se pudo confirmar la creacion de AITool {tool["name"]}: HTTP {status}, {str(doc)[:300]}')
    return str(doc['id'])


def main(argv=None, api_factory=OpenPages):
    parser = argparse.ArgumentParser(
        description='Inventario Langfuse JSON -> Agents, Tools y Models asociados en Consola de Gobierno SaaS')
    parser.add_argument('--input', default='inventario_langfuse.json')
    parser.add_argument('--parent-id', default=os.getenv('OP_PRIMARY_PARENT_ID', ''),
                        help='Resource ID del padre para crear agentes nuevos (Business Entity, Use Case o Model Group)')
    parser.add_argument('--max-agents', type=int, default=0,
                        help='0=todos; 1=primer agente y TODAS sus tools; N=primeros N agentes')
    parser.add_argument('--only-agent', default='',
                        help='Nombre o ID de un agente concreto del JSON')
    parser.add_argument('--skip-tools', action='store_true', help='Solo Agents + Models: omite las tools')
    parser.add_argument('--skip-models', action='store_true', help='Solo Agents + Tools: omite los modelos')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Consulta la consola, sin crear objetos')
    mode.add_argument('--apply', action='store_true', help='Crea agentes y tools, con deduplicacion')
    parser.add_argument('--report', default='resultado_importacion_agentes_tools.json')
    args = parser.parse_args(argv)
    if args.max_agents < 0:
        parser.error('--max-agents debe ser >= 0')

    agents, tools, source = inventory(args.input)
    models = models_from_json(args.input)
    if not agents:
        raise ValueError('El JSON no contiene agentes')
    if args.only_agent:
        selection = [a for a in agents if a['id'] == args.only_agent or a['name'].casefold() == args.only_agent.casefold()]
        if len(selection) != 1:
            raise ValueError(f'--only-agent no identifica exactamente un agente: {args.only_agent}')
        agents = selection
    if args.max_agents:
        agents = agents[:args.max_agents]
    print(f'Agentes seleccionados: {len(agents)} / herramientas en inventario: {len(tools)} / modelos: {len(models)}')
    for agent in agents:
        linked = [] if args.skip_tools else tools_of_agent(agent, tools)
        linked_models = [] if args.skip_models else models_of_agent(agent, models)
        print(f'  {agent["name"]} [{agent["id"]}]: tools={len(linked)}; modelos={len(linked_models)}')
        for m in linked_models:
            print(f'    Modelo relacionado: {m["name"]}')

    if not (args.apply or args.check):
        print('PREVIEW LOCAL: no se ha consultado ni modificado OpenPages.')
        return 0

    api = api_factory()
    agent_type_id = api.type_id()
    tool_type_id = type_id(api, 'AITool') if not args.skip_tools else None
    model_type_id = type_id(api, 'Model') if not args.skip_models else None
    agent_rows = api.existing_agents()
    tool_rows = (api.query_all('SELECT [Resource ID], [Name], [Description] FROM [AITool]')
                 if not args.skip_tools else [])
    model_rows = (api.query_all('SELECT [Resource ID], [Name], [Description] FROM [Model]')
                  if not args.skip_models else [])
    print(f'Conectado. AIComponent={agent_type_id}, AITool={tool_type_id}, Model={model_type_id}. '
          f'Existentes: {len(agent_rows)} agentes, {len(tool_rows)} tools y {len(model_rows)} modelos.')

    report = {'source': 'Langfuse', 'input': args.input,
              'mode': 'apply' if args.apply else 'check', 'results': []}
    if args.apply:
        # Si se han solicitado creaciones, exigimos un padre de referencia para agentes nuevos.
        # Los agentes existentes se localizan por marcador y no requieren el padre de creacion.
        need_parent = any(resolved_resource(agent_rows, AGENT_MARKER, a['id'], a['name'])[0] == 'new'
                          for a in agents)
        if need_parent and not args.parent_id:
            raise ValueError('Hay agentes nuevos: especifica --parent-id RESOURCE_ID para crearlos')
        if need_parent and not has_create_permission(api, args.parent_id, 'AIComponent'):
            raise APIError('No hay permiso para crear AIComponent bajo el parent-id indicado')

    for agent in agents:
        entry = {'agent': agent['name'], 'langfuse_agent_id': agent['id'], 'tools': [], 'models': []}
        report['results'].append(entry)
        status, agent_op_id = resolved_resource(agent_rows, AGENT_MARKER, agent['id'], agent['name'])
        if status == 'name_conflict':
            entry.update(status='name_conflict', detail=f'Objeto con mismo nombre pero sin marcador; ID {agent_op_id}')
            print(f'[CONFLICTO] {agent["name"]}: nombre ya registrado sin ID Langfuse; no se importan tools')
            save_json_safely(args.report, report)
            continue
        if status == 'new':
            if not args.apply:
                entry['status'] = 'would_create'
                print(f'[PLAN] Crear Agent {agent["name"]}')
            else:
                agent_op_id = api.create_agent(
                    agent['name'], agent_description(agent, source), args.parent_id, agent_type_id)
                entry.update(status='created', op_resource_id=agent_op_id)
                agent_rows.append({'resource id': agent_op_id, 'name': agent['name'],
                                   'description': agent_description(agent, source)})
                print(f'[CREADO] Agent {agent["name"]} -> {agent_op_id}')
                save_json_safely(args.report, report)
        else:
            entry.update(status='already_exists', op_resource_id=agent_op_id)
            print(f'[EXISTE] Agent {agent["name"]} -> {agent_op_id}')

        linked_tools = [] if args.skip_tools else tools_of_agent(agent, tools)
        if args.apply and any(resolved_resource(tool_rows, TOOL_MARKER, t['id'], t['name'])[0] == 'new' for t in linked_tools) and not has_create_permission(api, agent_op_id, 'AITool'):
            raise APIError(f'No tienes permisos para crear tools bajo el Agent {agent["name"]} (ID {agent_op_id})')

        for tool in linked_tools:
            tstatus, tool_op_id = resolved_resource(tool_rows, TOOL_MARKER, tool['id'], tool['name'])
            tool_entry = {'tool': tool['name'], 'langfuse_tool_id': tool['id']}
            entry['tools'].append(tool_entry)
            if tstatus == 'name_conflict':
                tool_entry.update(status='name_conflict', detail=f'Tool del mismo nombre sin marcador, ID {tool_op_id}')
                print(f'  [CONFLICTO] Tool {tool["name"]}: mismo nombre, no se modifica')
            elif tstatus == 'existing':
                tool_entry.update(status='already_exists', op_resource_id=tool_op_id)
                try:
                    parents = list_parent_ids(api, tool_op_id)
                    if agent_op_id and str(agent_op_id) not in parents:
                        tool_entry['status'] = 'association_mismatch'
                        tool_entry['detail'] = f'Tool existente pero el Agent esperado {agent_op_id} no figura entre sus padres: {sorted(parents)}'
                        print(f'  [REVISAR ASOCIACION] {tool["name"]} -> {tool_op_id}: padre esperado {agent_op_id}')
                    else:
                        print(f'  [EXISTE] Tool {tool["name"]} -> {tool_op_id} (asociacion verificada)' if agent_op_id else
                              f'  [EXISTE] Tool {tool["name"]} -> {tool_op_id}')
                except APIError as exc:
                    tool_entry['status'] = 'association_unverified'
                    tool_entry['detail'] = str(exc)
                    print(f'  [AVISO] Asociacion de {tool["name"]} no verificada: {exc}')
            elif not args.apply:
                tool_entry['status'] = 'would_create'
                print(f'  [PLAN] Crear Tool {tool["name"]} bajo Agent {agent["name"]}')
            else:
                new_tid = create_tool(api, tool, agent, source, agent_op_id, tool_type_id)
                tool_entry.update(status='created', op_resource_id=new_tid,
                                  parent_agent_op_id=agent_op_id)
                tool_rows.append({'resource id': new_tid, 'name': tool['name'],
                                  'description': tool_description(tool, agent, source)})
                save_json_safely(args.report, report)  # Guardar antes de verificar el enlace
                try:
                    parents = list_parent_ids(api, new_tid)
                    if str(agent_op_id) not in parents:
                        tool_entry['status'] = 'association_unverified'
                        tool_entry['detail'] = f'GET /associations no confirmo padre Agent {agent_op_id}; revisar UI'
                        print(f'  [AVISO] Tool {tool["name"]} creada, pero la asociacion no pudo verificarse')
                    else:
                        print(f'  [CREADO] Tool {tool["name"]} -> {new_tid} bajo Agent {agent_op_id}')
                except APIError as exc:
                    tool_entry['status'] = 'association_unverified'
                    tool_entry['detail'] = str(exc)
                    print(f'  [AVISO] Tool {tool["name"]} creada; no se pudo verificar relacion: {exc}')
            save_json_safely(args.report, report)

        linked_models = [] if args.skip_models else models_of_agent(agent, models)
        if args.apply and any(resolved_resource(model_rows, MODEL_MARKER, m['id'], m['name'])[0] == 'new' for m in linked_models):
            if not has_create_permission(api, agent_op_id, 'Model'):
                raise APIError(f'No hay permiso o asociacion permitida para crear Model bajo Agent {agent["name"]} ({agent_op_id}).')
        for model in linked_models:
            mstatus, model_op_id = resolved_resource(model_rows, MODEL_MARKER, model['id'], model['name'])
            model_entry = {'model': model['name'], 'langfuse_model_id': model['id']}
            entry['models'].append(model_entry)
            if mstatus == 'name_conflict':
                model_entry.update(status='name_conflict', detail=f'Ya existe un Model con el nombre, ID {model_op_id}, sin marcador Langfuse coincidente; no se vincula automáticamente.')
                print(f'  [CONFLICTO] Model {model["name"]}: nombre ya existe sin marcador; no se cambia')
            elif mstatus == 'existing':
                model_entry.update(status='already_exists', op_resource_id=model_op_id)
                try:
                    parents = list_parent_ids(api, model_op_id)
                    if agent_op_id and str(agent_op_id) not in parents:
                        model_entry['status'] = 'association_mismatch'
                        model_entry['detail'] = f'El agente {agent_op_id} no figura entre los padres: {sorted(parents)}; revisar manualmente.'
                        print(f'  [REVISAR ASOCIACION] Model {model["name"]} existe, pero no vinculado a este Agent')
                    else:
                        print(f'  [EXISTE] Model {model["name"]} -> {model_op_id} (asociacion verificada)')
                except APIError as exc:
                    model_entry.update(status='association_unverified', detail=str(exc))
                    print(f'  [AVISO] Asociacion del modelo no verificada: {exc}')
            elif not args.apply:
                model_entry['status'] = 'would_create'
                print(f'  [PLAN] Crear Model {model["name"]} bajo Agent {agent["name"]}')
            else:
                new_mid = create_model(api, model, agent, source, agent_op_id, model_type_id)
                model_entry.update(status='created', op_resource_id=new_mid,
                                   parent_agent_op_id=agent_op_id)
                model_rows.append({'resource id': new_mid, 'name': model['name'],
                                   'description': model_description(model, agent, source)})
                save_json_safely(args.report, report)
                try:
                    parents = list_parent_ids(api, new_mid)
                    if str(agent_op_id) not in parents:
                        model_entry.update(status='association_unverified', detail=f'GET /associations no confirma padre Agent {agent_op_id}; revisar en interfaz')
                        print(f'  [AVISO] Model {model["name"]} creado, pero enlace no confirmado')
                    else:
                        print(f'  [CREADO] Model {model["name"]} -> {new_mid} bajo Agent {agent_op_id}')
                except APIError as exc:
                    model_entry.update(status='association_unverified', detail=str(exc))
                    print(f'  [AVISO] Model {model["name"]} creado; no se pudo confirmar enlace: {exc}')
            save_json_safely(args.report, report)
        save_json_safely(args.report, report)

    print(f'Finalizado. Informe: {args.report}')
    if any(e['status'] in ('name_conflict', 'association_mismatch', 'association_unverified')
           for r in report['results'] for e in (r['tools'] + r['models'])):
        print('ATENCION: hay conflictos o asociaciones pendientes de revisar. Consulta el informe.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, APIError, OSError, json.JSONDecodeError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        raise SystemExit(1)

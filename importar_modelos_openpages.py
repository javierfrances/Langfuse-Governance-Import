#!/usr/bin/env python3
"""Funciones de modelos para importador combinado Langfuse -> Consola de Gobierno.

Modelo de gobierno: objeto Model, hijo de AIComponent (Agent).
No registra ni despliega un modelo ejecutable.
"""
import json
from pathlib import Path
from importar_agentes_openpages import APIError
from importar_tools_openpages import type_id

MODEL_TYPE = 'Model'
MODEL_MARKER = 'Langfuse foundation model inventory ID: '
PARENT_MARKER = 'Parent Langfuse agent ID: '


def models_from_json(path):
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    models = (document.get('inventory') or {}).get('foundation_models', [])
    if not isinstance(models, list):
        raise ValueError('inventory.foundation_models debe ser una lista')
    seen = set()
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get('id'), str) or not isinstance(model.get('name'), str):
            raise ValueError('Modelo sin id o name valido')
        ident = model['id'].strip()
        if not ident or not model['name'].strip() or ident in seen:
            raise ValueError('Modelo con id/nombre vacio o id duplicado')
        seen.add(ident)
    return models


def models_of_agent(agent, models):
    agent_id = agent['id']
    referenced = set((agent.get('relationships') or {}).get('model_ids') or [])
    return [m for m in models if m['id'] in referenced or agent_id in
            set((m.get('relationships') or {}).get('agent_ids') or [])]


def model_description(model, agent, source):
    identity = model.get('identity') or {}
    attributes = model.get('attributes') or {}
    evidence = model.get('first_seen') or {}
    provider = (identity.get('provider') or attributes.get('provider') or '') if isinstance(identity, dict) and isinstance(attributes, dict) else ''
    parts = [
        '[Modelo fundacional observado en trazas de Langfuse - registro de gobierno, no despliegue]',
        MODEL_MARKER + model['id'],
        PARENT_MARKER + agent['id'],
        'Source: Langfuse',
        'Langfuse project ID: ' + str(source.get('project_id', 'unknown')),
        'Original model name: ' + model['name'],
    ]
    if provider:
        parts.append('Model provider: ' + str(provider))
    if isinstance(evidence, dict) and evidence.get('trace_id'):
        parts.append('Evidence trace ID: ' + str(evidence['trace_id']))
    return '\n'.join(parts)


def create_model(api, model, agent, source, agent_op_id, model_type_id):
    payload = {
        'name': model['name'].strip(),
        'description': model_description(model, agent, source),
        'type_definition_id': str(model_type_id),
        'primary_parent_id': str(agent_op_id),
        'fields': [],
    }
    status, response = api.request('POST', '/contents', payload)
    if status != 201 or not isinstance(response, dict) or not response.get('id'):
        raise APIError('No se ha confirmado la creacion de Model ' + model['name'] +
                       f' (HTTP {status}; respuesta={str(response)[:350]})')
    return str(response['id'])

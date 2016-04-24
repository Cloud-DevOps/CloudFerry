# Copyright (c) 2016 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the License);
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an AS IS BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and#
# limitations under the License.
import os

import futurist
from taskflow import engines
from taskflow import exceptions
from taskflow.patterns import graph_flow
from taskflow.persistence import backends
from taskflow.persistence import models

TASK_DATABASE_FILE = os.environ.get('CF_TASK_DB', './tasks.db')
LOGBOOK_ID = 'primary'
MAX_WORKERS = int(os.environ.get('CF_MAX_WORKERS', 4))


def _ensure_db_initialized(conn, flow):
    conn.upgrade()

    need_save = False
    try:
        logbook = conn.get_logbook(LOGBOOK_ID)
    except exceptions.NotFound:
        logbook = models.LogBook(LOGBOOK_ID, uuid=LOGBOOK_ID)
        need_save = True

    flow_detail = logbook.find(flow.name)
    if flow_detail is None:
        flow_detail = models.FlowDetail(flow.name, flow.name)
        logbook.add(flow_detail)
        need_save = True

    if need_save:
        conn.save_logbook(logbook)

    return logbook, flow_detail


def _workaround_reverted_reset(flow_detail):
    flow_detail.state = 'PENDING'
    for task_detail in flow_detail:
        if task_detail.state == 'REVERTED':
            task_detail.reset('PENDING')


def execute_flow(flow):
    backend = backends.fetch({
        'connection': 'sqlite:///' + TASK_DATABASE_FILE,
        'isolation_level': 'SERIALIZABLE'
    })
    executor = futurist.ThreadPoolExecutor(max_workers=MAX_WORKERS)
    conn = backend.get_connection()
    logbook, flow_detail = _ensure_db_initialized(conn, flow)
    engine = engines.load(
        flow, flow_detail=flow_detail, backend=backend, book=logbook,
        engine='parallel', executor=executor)

    engine.compile()
    _workaround_reverted_reset(flow_detail)
    engine.run()


def create_graph_flow(name, objs, subflow_factory_fn, *args, **kwargs):
    def _create_and_link_subflow(obj):
        obj_id = obj.primary_key
        if obj_id in created:
            return created[obj_id]
        subflow = subflow_factory_fn(obj, *args, **kwargs)
        if subflow is None:
            return None
        graph.add(subflow)
        created[obj_id] = subflow
        for dep in obj.dependencies():
            dep_subflow = _create_and_link_subflow(dep)
            if dep_subflow is not None:
                graph.link(dep_subflow, subflow)
        return subflow

    created = {}
    graph = graph_flow.Flow(name)
    for obj in objs:
        _create_and_link_subflow(obj)
    return graph


def object_name(obj):
    object_id = obj.primary_key
    return '{typename}_{cloud}_{uuid}'.format(
        typename=obj.get_class_qualname(),
        cloud=object_id.cloud,
        uuid=object_id.id)


def map_object_id(obj, cloud):
    link = obj.find_link(cloud.name)
    assert link is not None
    return link.primary_key.id
#
# Copyright (c) 2020, Neptune Labs Sp. z o.o.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import logging
import os
from typing import Optional, Union, Iterable

from neptune.new.envs import PROJECT_ENV_NAME
from neptune.new.exceptions import NeptuneMissingProjectNameException
from neptune.new.internal.backends.hosted_neptune_backend import HostedNeptuneBackend
from neptune.new.internal.credentials import Credentials
from neptune.new.internal.utils import verify_type
from neptune.new.experiments_table import ExperimentsTable
from neptune.new.project import Project
from neptune.new.version import version as parsed_version

__version__ = str(parsed_version)

_logger = logging.getLogger(__name__)


# pylint:disable=redefined-builtin
def get_experiments_table(id: Optional[Union[str, Iterable[str]]] = None,
                          state: Optional[Union[str, Iterable[str]]] = None,
                          owner: Optional[Union[str, Iterable[str]]] = None,
                          tag: Optional[Union[str, Iterable[str]]] = None
                          ) -> ExperimentsTable:
    return get_project().get_experiments_table(id=id, state=state, owner=owner, tag=tag)


def get_project(name: Optional[str] = None) -> Project:
    verify_type("name", name, (str, type(None)))

    if not name:
        name = os.getenv(PROJECT_ENV_NAME)
    if not name:
        raise NeptuneMissingProjectNameException()

    backend = HostedNeptuneBackend(Credentials())

    project_obj = backend.get_project(name)

    return Project(project_obj.uuid, backend)
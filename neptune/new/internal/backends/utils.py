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
import itertools
import logging
import os
import socket
import sys
import time
from functools import lru_cache, wraps
from typing import (
    Optional,
    Dict,
    TYPE_CHECKING,
    Mapping,
    Text,
    Any,
    List,
    Iterable,
)
from urllib.parse import urlparse, urljoin


import click
import requests
import urllib3
from bravado.requests_client import RequestsResponseAdapter
from urllib3.exceptions import NewConnectionError
from bravado.client import SwaggerClient
from bravado.exception import (
    BravadoConnectionError,
    BravadoTimeoutError,
    HTTPForbidden,
    HTTPServerError,
    HTTPUnauthorized,
    HTTPServiceUnavailable,
    HTTPRequestTimeout,
    HTTPGatewayTimeout,
    HTTPBadGateway,
    HTTPClientError,
    HTTPTooManyRequests,
    HTTPError,
)
from bravado.http_client import HttpClient
from bravado_core.formatter import SwaggerFormat
from packaging.version import Version
from requests import Session, Response

from neptune.new.envs import (
    NEPTUNE_RETRIES_TIMEOUT_ENV,
    NEPTUNE_ALLOW_SELF_SIGNED_CERTIFICATE,
)
from neptune.new.exceptions import (
    SSLError,
    NeptuneConnectionLostException,
    Unauthorized,
    Forbidden,
    CannotResolveHostname,
    UnsupportedClientVersion,
    ClientHttpError,
    NeptuneFeatureNotAvailableException,
    MetadataInconsistency,
    NeptuneInvalidApiTokenException,
)
from neptune.new.internal.backends.api_model import ClientConfig
from neptune.new.internal.operation import Operation, CopyAttribute
from neptune.new.internal.utils import replace_patch_version

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from neptune.new.internal.backends.neptune_backend import NeptuneBackend
    from neptune.new.internal.backends.hosted_neptune_backend import (
        HostedNeptuneBackend,
    )

MAX_RETRY_TIME = 30
retries_timeout = int(os.getenv(NEPTUNE_RETRIES_TIMEOUT_ENV, "60"))


def with_api_exceptions_handler(func):
    def wrapper(*args, **kwargs):
        last_exception = None
        start_time = time.monotonic()
        for retry in itertools.count(0):
            if time.monotonic() - start_time > retries_timeout:
                break

            try:
                return func(*args, **kwargs)
            except requests.exceptions.InvalidHeader as e:
                if "X-Neptune-Api-Token" in e.args[0]:
                    raise NeptuneInvalidApiTokenException()
                raise
            except requests.exceptions.SSLError as e:
                raise SSLError() from e
            except (
                BravadoConnectionError,
                BravadoTimeoutError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                HTTPRequestTimeout,
                HTTPServiceUnavailable,
                HTTPGatewayTimeout,
                HTTPBadGateway,
                HTTPTooManyRequests,
                HTTPServerError,
                NewConnectionError,
            ) as e:
                time.sleep(min(2 ** min(10, retry), MAX_RETRY_TIME))
                last_exception = e
                continue
            except HTTPUnauthorized:
                raise Unauthorized()
            except HTTPForbidden:
                raise Forbidden()
            except HTTPClientError as e:
                raise ClientHttpError(e.status_code, e.response.text) from e
            except requests.exceptions.RequestException as e:
                if e.response is None:
                    raise
                status_code = e.response.status_code
                if status_code in (
                    HTTPRequestTimeout.status_code,
                    HTTPBadGateway.status_code,
                    HTTPServiceUnavailable.status_code,
                    HTTPGatewayTimeout.status_code,
                    HTTPTooManyRequests.status_code,
                    HTTPServerError.status_code,
                ):
                    time.sleep(min(2 ** min(10, retry), MAX_RETRY_TIME))
                    last_exception = e
                    continue
                elif status_code == HTTPUnauthorized.status_code:
                    raise Unauthorized()
                elif status_code == HTTPForbidden.status_code:
                    raise Forbidden()
                elif 400 <= status_code < 500:
                    raise ClientHttpError(status_code, e.response.text) from e
                else:
                    raise
        raise NeptuneConnectionLostException(last_exception) from last_exception

    return wrapper


@lru_cache(maxsize=None, typed=True)
def verify_host_resolution(url: str) -> None:
    host = urlparse(url).netloc.split(":")[0]
    try:
        socket.gethostbyname(host)
    except socket.gaierror:
        raise CannotResolveHostname(host)


uuid_format = SwaggerFormat(
    format="uuid",
    to_python=lambda x: x,
    to_wire=lambda x: x,
    validate=lambda x: None,
    description="",
)


@with_api_exceptions_handler
def create_swagger_client(url: str, http_client: HttpClient) -> SwaggerClient:
    return SwaggerClient.from_url(
        url,
        config=dict(
            validate_swagger_spec=False,
            validate_requests=False,
            validate_responses=False,
            formats=[uuid_format],
        ),
        http_client=http_client,
    )


def verify_client_version(client_config: ClientConfig, version: Version):
    version_with_patch_0 = Version(replace_patch_version(str(version)))
    if (
        client_config.version_info.min_compatible
        and client_config.version_info.min_compatible > version
    ):
        raise UnsupportedClientVersion(
            version, min_version=client_config.version_info.min_compatible
        )
    if (
        client_config.version_info.max_compatible
        and client_config.version_info.max_compatible < version_with_patch_0
    ):
        raise UnsupportedClientVersion(
            version, max_version=client_config.version_info.max_compatible
        )
    if (
        client_config.version_info.min_recommended
        and client_config.version_info.min_recommended > version
    ):
        click.echo(
            "WARNING: We recommend an upgrade to a new version of neptune-client - {} (installed - {}).".format(
                client_config.version_info.min_recommended, version
            ),
            sys.stderr,
        )


def update_session_proxies(session: Session, proxies: Optional[Dict[str, str]]):
    if proxies:
        try:
            session.proxies.update(proxies)
        except (TypeError, ValueError):
            raise ValueError("Wrong proxies format: {}".format(proxies))


def build_operation_url(base_api: str, operation_url: str) -> str:
    if "://" not in base_api:
        base_api = f"https://{base_api}"

    return urljoin(base=base_api, url=operation_url)


# TODO print in color once colored exceptions are added
def handle_server_raw_response_messages(response: Response):
    try:
        info = response.headers.get("X-Server-Info")
        if info:
            click.echo(info)
        warning = response.headers.get("X-Server-Warning")
        if warning:
            click.echo(warning)
        error = response.headers.get("X-Server-Error")
        if error:
            click.echo(message=error, err=True)
        return response
    except Exception:
        # any issues with printing server messages should not cause code to fail
        return response


# TODO print in color once colored exceptions are added
class NeptuneResponseAdapter(RequestsResponseAdapter):
    @property
    def raw_bytes(self) -> bytes:
        self._handle_response()
        return super().raw_bytes

    @property
    def text(self) -> Text:
        self._handle_response()
        return super().text

    def json(self, **kwargs) -> Mapping[Text, Any]:
        self._handle_response()
        return super().json(**kwargs)

    def _handle_response(self):
        try:
            info = self._delegate.headers.get("X-Server-Info")
            if info:
                click.echo(info)
            warning = self._delegate.headers.get("X-Server-Warning")
            if warning:
                click.echo(warning)
            error = self._delegate.headers.get("X-Server-Error")
            if error:
                click.echo(message=error, err=True)
        except Exception:
            # any issues with printing server messages should not cause code to fail
            pass


class MissingApiClient(SwaggerClient):
    """catch-all class to gracefully handle calls to unavailable API"""

    def __init__(self, feature_name: str):  # pylint: disable=super-init-not-called
        self.feature_name = feature_name

    def __getattr__(self, item):
        raise NeptuneFeatureNotAvailableException(missing_feature=self.feature_name)


# https://stackoverflow.com/a/44776960
def cache(func):
    """
    Transform mutable dictionary into immutable before call to lru_cache
    """

    class HDict(dict):
        def __hash__(self):
            return hash(frozenset(self.items()))

    func = lru_cache(maxsize=None, typed=True)(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        args = tuple([HDict(arg) if isinstance(arg, dict) else arg for arg in args])
        kwargs = {k: HDict(v) if isinstance(v, dict) else v for k, v in kwargs.items()}
        return func(*args, **kwargs)

    wrapper.cache_clear = func.cache_clear
    return wrapper


def ssl_verify():
    if os.getenv(NEPTUNE_ALLOW_SELF_SIGNED_CERTIFICATE):
        urllib3.disable_warnings()
        return False

    return True


def parse_validation_errors(error: HTTPError) -> Dict[str, str]:
    return {
        f"{error_description.get('errorCode').get('name')}": error_description.get(
            "context", ""
        )
        for validation_error in error.swagger_result.validationErrors
        for error_description in validation_error.get("errors")
    }


class ExecuteOperationsBatchingManager:
    def __init__(self, backend: "NeptuneBackend"):
        self._backend = backend

    def get_batch(
        self, ops: Iterable[Operation], errors: List[MetadataInconsistency]
    ) -> List[Operation]:
        batch = []
        for op in ops:
            if isinstance(op, CopyAttribute):
                if not batch:
                    try:
                        # CopyAttribute can be at the start of a batch
                        batch.append(op.resolve(self._backend))
                    except MetadataInconsistency as e:
                        errors.append(e)
                else:
                    # cannot have CopyAttribute after any other op in a batch
                    break
            else:
                batch.append(op)

        return batch

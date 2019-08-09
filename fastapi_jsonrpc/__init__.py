import asyncio
import inspect
import logging
import sys
from json import JSONDecodeError
from types import FunctionType, CoroutineType
from typing import List, Union, Any, Callable, Type, Optional, Dict, Sequence, Awaitable

from fastapi.dependencies.models import Dependant
from fastapi.encoders import jsonable_encoder
from fastapi.params import Depends
from pydantic import BaseModel, Schema, StrictStr, ValidationError, DictError
from fastapi import FastAPI
from fastapi.dependencies.utils import solve_dependencies, get_dependant, add_non_field_param_to_dependency
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute, APIRouter, serialize_response
from pydantic.main import MetaModel
from pydantic.utils import resolve_annotations
from starlette.background import BackgroundTasks
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, request_response
import fastapi.params
import aiojobs


logger = logging.getLogger(__name__)


components = {}


def component_name(name: str, module: str = None):
    """OpenAPI components must be unique by name"""
    def decorator(obj):
        obj.__name__ = name
        if module is not None:
            obj.__module__ = module  # see: pydantic.schema.get_long_model_name
        key = (obj.__name__, obj.__module__)
        if key in components:
            if components[key].schema() != obj.schema():
                raise RuntimeError(
                    f'Different models with the same name detected: {obj!r} != {components[key]}'
                )
            return components[key]
        components[key] = obj
        return obj
    return decorator


def is_scope_child(owner: type, child: type):
    return (
        child.__qualname__ == owner.__qualname__ + '.' + child.__name__ and
        child.__module__ == owner.__module__
    )


class BaseError(Exception):
    CODE = None
    MESSAGE = None

    ErrorModel = None
    DataModel = None

    error_model = None
    data_model = None
    resp_model = None

    _component_name = None

    def __init__(self, data=None, req=None):
        if data is None:
            data = {}
        Exception.__init__(self, self.CODE, self.MESSAGE)
        self.data = data
        self.req = req

    def __str__(self):
        s = f'[{self.CODE}] {self.MESSAGE}'
        if self.data:
            s += f': {self.data!r}'
        return s

    def bind_req(self, req):
        self.req = req

    def get_resp_data(self):
        return self.data

    @classmethod
    def get_description(cls):
        s = cls.get_default_description()
        if cls.__doc__:
            s += '\n\n' + cls.__doc__
        return s

    @classmethod
    def get_default_description(cls):
        return f'[{cls.CODE}] {cls.MESSAGE}'

    def get_resp(self):
        error = {
            'code': self.CODE,
            'message': self.MESSAGE,
        }

        resp_data = self.get_resp_data()
        if resp_data:
            error['data'] = resp_data

        resp = {
            'jsonrpc': '2.0',
            'error': error,
        }

        if isinstance(self.req, dict):
            resp['id'] = self.req.get('id')
        else:
            resp['id'] = None

        return jsonable_encoder(resp)

    @classmethod
    def get_error_model(cls):
        if cls.__dict__.get('error_model') is not None:
            return cls.error_model
        cls.error_model = cls.build_error_model()
        return cls.error_model

    @classmethod
    def build_error_model(cls):
        if cls.ErrorModel is not None:
            if is_scope_child(cls, cls.ErrorModel):
                return component_name(f'{cls.__name__}.Error', cls.__module__)(cls.ErrorModel)
        return cls.ErrorModel

    @classmethod
    def get_data_model(cls):
        if cls.__dict__.get('data_model') is not None:
            return cls.data_model
        cls.data_model = cls.build_data_model()
        return cls.data_model

    @classmethod
    def build_data_model(cls):
        if cls.DataModel is not None:
            if is_scope_child(cls, cls.DataModel):
                return component_name(f'{cls.__name__}.Data', cls.__module__)(cls.DataModel)
            return cls.DataModel

        error_model = cls.get_error_model()
        if error_model is None:
            return None

        @component_name(f'_ErrorData[{error_model.__name__}]', error_model.__module__)
        class _ErrorData(BaseModel):
            errors: List[error_model]

        return _ErrorData

    @classmethod
    def get_resp_model(cls):
        if cls.__dict__.get('resp_model') is not None:
            return cls.resp_model
        cls.resp_model = cls.build_resp_model()
        return cls.resp_model

    @classmethod
    def build_resp_model(cls):
        ns = {
            'code': Schema(cls.CODE, const=True, example=cls.CODE),
            'message': Schema(cls.MESSAGE, const=True, example=cls.MESSAGE),
            '__annotations__': {
                'code': int,
                'message': str,
            }
        }

        data_model = cls.get_data_model()
        if data_model is not None:
            # noinspection PyTypeChecker
            ns['__annotations__']['data'] = data_model

        name = cls._component_name or cls.__name__

        _JsonRpcErrorModel = MetaModel.__new__(MetaModel, '_JsonRpcErrorModel', (BaseModel, ), ns)
        _JsonRpcErrorModel = component_name(name, cls.__module__)(_JsonRpcErrorModel)

        @component_name(f'_ErrorResponse[{name}]', cls.__module__)
        class _ErrorResponseModel(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            error: _JsonRpcErrorModel

            class Config:
                extra = 'forbid'

        return _ErrorResponseModel


@component_name('_Error')
class ErrorModel(BaseModel):
    loc: List[str]
    msg: str
    type: str
    ctx: Optional[Dict[str, Any]]


class ParseError(BaseError):
    """Invalid JSON was received by the server"""
    CODE = -32700
    MESSAGE = 'Parse error'


class InvalidRequest(BaseError):
    """The JSON sent is not a valid Request object"""
    CODE = -32600
    MESSAGE = 'Invalid Request'
    error_model = ErrorModel


class MethodNotFound(BaseError):
    """The method does not exist / is not available"""
    CODE = -32601
    MESSAGE = 'Method not found'


class InvalidParams(BaseError):
    """Invalid method parameter(s)"""
    CODE = -32602
    MESSAGE = 'Invalid params'
    error_model = ErrorModel


class InternalError(BaseError):
    """Internal JSON-RPC error"""
    CODE = -32603
    MESSAGE = 'Internal error'


async def call_sync_async(call, *args, **kwargs):
    is_coroutine = asyncio.iscoroutinefunction(call)
    if is_coroutine:
        return await call(*args, **kwargs)
    else:
        return await run_in_threadpool(call, *args, **kwargs)


class Param(fastapi.params.Body):
    pass


def errors_responses(errors: Sequence[Type[BaseError]] = None):
    responses = {}

    if errors:
        cnt = 1
        for error_cls in errors:
            responses[f'200{" " * cnt}'] = {
                'model': error_cls.get_resp_model(),
                'description': error_cls.get_description(),
            }
            cnt += 1

    return responses


def request_validation_error(
    exc: ValidationError,
) -> Union[InvalidRequest, InvalidParams]:
    params_errors = []
    request_errors = []

    for err in exc.errors():
        if err['loc'][:3] == ('body', '__request__', 'params') and len(err['loc']) > 3:
            err['loc'] = err['loc'][3:]
            target = params_errors
        elif err['loc'][:2] == ('body', '__request__'):
            err['loc'] = err['loc'][2:]
            target = request_errors
        else:
            # Errors in the parameters `query`, `cookies`, `headers`, etc. are interpreted as InvalidRequest
            target = request_errors

        target.append(err)

    if request_errors:
        error = InvalidRequest(data={'errors': request_errors})
    else:
        error = InvalidParams(data={'errors': params_errors})

    return error


class MethodRoute(APIRoute):
    def __init__(
        self,
        entrypoint: 'Entrypoint',
        path: str,
        func: Union[FunctionType, CoroutineType],
        *,
        result_model: Type[Any] = None,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        shared_dependencies: list = None,
        **kwargs,
    ):
        name = name or func.__name__
        result_model = result_model or func.__annotations__.get('return')

        parameters = inspect.signature(func).parameters

        if '__request__' in parameters:
            raise RuntimeError("Parameter name '__request__' reserved, please use other name")

        for param_name, param in parameters.items():
            if isinstance(param.default, Param):
                continue
            if isinstance(param.default, fastapi.params.Body):
                # body is already taken by the body of the JSON-RPC request
                raise RuntimeError(f"There can be no Body-parameters for the JSON-RPC method: {param_name}")

        annotations = func.__annotations__
        if sys.version_info >= (3, 7):
            annotations = resolve_annotations(annotations, func.__module__)

        def is_jsonrpc_param(n):
            p = parameters[n]
            if p.default == p.empty:
                if n in annotations:
                    dummy_dependant = Dependant()
                    if isinstance(p.annotation, str):
                        p = p.replace(annotation=annotations[n])
                    if add_non_field_param_to_dependency(param=p, dependant=dummy_dependant):
                        return False
                return True
            if isinstance(p.default, Param):
                return True
            return False

        ns = {n: p.default for n, p in parameters.items() if p.default != p.empty and is_jsonrpc_param(n)}
        ns['__annotations__'] = {n: v for n, v in annotations.items() if n != 'return' and is_jsonrpc_param(n)}

        _JsonRpcRequestParams = MetaModel.__new__(MetaModel, '_JsonRpcRequestParams', (BaseModel, ), ns)
        _JsonRpcRequestParams = component_name(f'_Params[{name}]', func.__module__)(_JsonRpcRequestParams)

        @component_name(f'_Request[{name}]', func.__module__)
        class _Request(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            method: StrictStr = Schema(name, const=True, example=name)
            params: _JsonRpcRequestParams

            class Config:
                extra = 'forbid'

        @component_name(f'_Response[{name}]', func.__module__)
        class _Response(BaseModel):
            jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
            id: Union[StrictStr, int] = Schema(None, example=0)
            result: result_model

            class Config:
                extra = 'forbid'

        # Only needed to generate OpenAPI
        async def endpoint(__request__: _Request):
            del __request__

        endpoint.__name__ = func.__name__
        endpoint.__doc__ = func.__doc__

        responses = errors_responses(errors)

        super().__init__(
            path,
            endpoint,
            methods=['POST'],
            name=name,
            response_model=_Response,
            response_model_skip_defaults=True,
            responses=responses,
            **kwargs,
        )

        # Add dependencies and other parameters from func
        dependant = get_dependant(path=self.path_format, call=func)
        self.dependant.path_params = dependant.path_params
        self.dependant.query_params = [p for p in dependant.query_params if not is_jsonrpc_param(p.name)]
        self.dependant.header_params = dependant.header_params
        self.dependant.cookie_params = dependant.cookie_params
        self.dependant.dependencies = dependant.dependencies
        self.dependant.security_requirements = dependant.security_requirements
        self.dependant.request_param_name = dependant.request_param_name
        self.dependant.websocket_param_name = dependant.websocket_param_name
        self.dependant.response_param_name = dependant.response_param_name
        self.dependant.background_tasks_param_name = dependant.background_tasks_param_name
        self.dependant.security_scopes = dependant.security_scopes
        self.dependant.security_scopes_param_name = dependant.security_scopes_param_name

        self.func = func
        self.entrypoint = entrypoint
        self.app = request_response(self.jsonrpc_app_response)
        self.shared_dependencies = shared_dependencies

    def get_error_resp(self, error: BaseError):
        return self.entrypoint.get_error_resp(error)

    async def jsonrpc_app_response(self, http_request: Request):
        background_tasks = BackgroundTasks()

        # There may be exceptions to the transport layer, we don’t wrap them in JSON-RPC
        dependency_cache = await self.entrypoint.solve_shared_dependencies(http_request, background_tasks)

        try:
            req = await http_request.json()
        except JSONDecodeError:
            content = self.get_error_resp(ParseError())
        else:
            try:
                content = await self.jsonrpc_app_content(http_request, background_tasks, dependency_cache, req)
            except NoContent:
                return Response(media_type='application/json', background=background_tasks)

        return self.response_class(content=content, background=background_tasks)

    async def jsonrpc_app_content(
        self, http_request: Request, background_tasks: BackgroundTasks, dependency_cache, req,
    ):
        try:
            resp = await self.jsonrpc_app(http_request, background_tasks, dependency_cache, req)
        except BaseError as error:
            error.bind_req(req)
            resp = self.get_error_resp(error)

        if 'error' in resp or 'id' in req:
            return resp
        else:
            # No content for notification
            raise NoContent

    async def jsonrpc_app(self, http_request: Request, background_tasks: BackgroundTasks, dependency_cache, req):
        try:
            # dependency_cache - these are transport-layer dependencies, we pass them to each method, since
            # they are common to all methods in the batch.
            # But if the methods have their own dependencies, they are resolved separately.
            dependency_cache = dependency_cache.copy()
            values, errors, background_tasks, sub_response, _ = await solve_dependencies(
                request=http_request,
                dependant=self.dependant,
                body=req,
                background_tasks=background_tasks,
                dependency_overrides_provider=self.dependency_overrides_provider,
                dependency_cache=dependency_cache,
            )

            if errors:
                raise request_validation_error(RequestValidationError(errors))

            request = values.pop('__request__')  # _Request

            result = await call_sync_async(self.func, **request.params.dict(), **values)
        except BaseError as error:
            raise error
        except Exception as exc:
            logger.exception(str(exc), exc_info=exc)
            raise InternalError()

        resp = {
            'jsonrpc': '2.0',
            'result': result,
            'id': req.get('id')
        }

        # noinspection PyTypeChecker
        response_data = serialize_response(
            field=self.secure_cloned_response_field,
            response=resp,
            include=self.response_model_include,
            exclude=self.response_model_exclude,
            by_alias=self.response_model_by_alias,
            skip_defaults=self.response_model_skip_defaults,
        )

        return response_data


@component_name(f'_Request')
class JsonRpcRequest(BaseModel):
    jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
    id: Union[StrictStr, int] = Schema(None, example=0)
    method: StrictStr
    params: dict

    class Config:
        extra = 'forbid'


@component_name(f'_Response')
class JsonRpcResponse(BaseModel):
    jsonrpc: StrictStr = Schema('2.0', const=True, example='2.0')
    id: Union[StrictStr, int] = Schema(None, example=0)
    result: dict

    class Config:
        extra = 'forbid'


class NoContent(Exception):
    pass


class EntrypointRoute(APIRoute):
    def __init__(
        self,
        entrypoint: 'Entrypoint',
        path: str,
        *,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        **kwargs,
    ):
        name = name or 'entrypoint'

        # This is only necessary for generating OpenAPI
        def endpoint(__request__: JsonRpcRequest):
            del __request__

        responses = errors_responses(errors)

        super().__init__(
            path,
            endpoint,
            methods=['POST'],
            name=name,
            response_model=JsonRpcResponse,
            responses=responses,
            **kwargs,
        )

        self.app = request_response(self.jsonrpc_app_response)
        self.entrypoint = entrypoint

    async def solve_dependencies(self, http_request: Request, background_tasks: BackgroundTasks):
        # Must not be empty, otherwise FastAPI re-creates it
        dependency_cache = {(lambda: None, ('', )): 1}
        if self.dependencies:
            await solve_dependencies(
                request=http_request,
                dependant=self.dependant,
                body=None,
                background_tasks=background_tasks,
                dependency_overrides_provider=self.dependency_overrides_provider,
                dependency_cache=dependency_cache,
            )
        return dependency_cache

    async def jsonrpc_app_response(self, http_request: Request):
        background_tasks = BackgroundTasks()

        # There may be exceptions to the transport layer, we don’t wrap them in JSON-RPC
        dependency_cache = await self.solve_dependencies(http_request, background_tasks)

        try:
            content = await self._entrypoint(http_request, background_tasks, dependency_cache)
        except NoContent:
            return Response(media_type='application/json', background=background_tasks)
        else:
            return self.response_class(content=content, background=background_tasks)

    def get_error_resp(self, error: BaseError):
        return self.entrypoint.get_error_resp(error)

    async def _entrypoint(
        self,
        http_request: Request,
        background_tasks: BackgroundTasks,
        dependency_cache,
    ):
        try:
            body = await http_request.json()
        except JSONDecodeError:
            return self.get_error_resp(ParseError())

        if not isinstance(body, list):
            req_list = [body]
        else:
            req_list = body
            if not req_list:
                error = InvalidRequest(data={'errors': [
                    {'loc': (), 'type': 'value_error.empty', 'msg': 'rpc call with an empty array'}
                ]})
                return self.get_error_resp(error)

        scheduler = await self.entrypoint.get_scheduler()

        job_list = []
        if len(req_list) > 1:
            # Run concurrently through scheduler
            for req in req_list:
                job = await scheduler.spawn(
                    self._single_req_job(http_request, background_tasks, dependency_cache, req)
                )

                # TODO: https://github.com/aio-libs/aiojobs/issues/119
                job._explicit = True
                # noinspection PyProtectedMember
                task = job._do_wait(timeout=None)

                job_list.append((req, task))
        else:
            req = req_list[0]
            task = self._single_req_job(http_request, background_tasks, dependency_cache, req)
            job_list.append((req, task))

        resp_list = []

        for req, task in job_list:
            try:
                resp = await task
            except BaseError as error:
                error.bind_req(req)
                resp = self.get_error_resp(error)
            except NoContent:
                # No content for notification
                continue

            resp_list.append(resp)

        if not resp_list:
            raise NoContent

        if not isinstance(body, list):
            content = resp_list[0]
        else:
            content = resp_list

        return content

    async def _single_req_job(self, http_request, background_tasks, dependency_cache, req):
        scope = http_request.scope.copy()

        try:
            JsonRpcRequest.validate(req)
        except DictError:
            raise InvalidRequest(data={'errors': [
                {'loc': (), 'type': 'type_error.dict', 'msg': 'value is not a valid dict'}
            ]})
        except ValidationError as exc:
            raise request_validation_error(exc)

        scope['path'] = self.path + '/' + req['method']

        for route in self.entrypoint.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                # http_request is a transport layer and it is common for all JSON-RPC requests in a batch
                return await route.jsonrpc_app_content(http_request, background_tasks, dependency_cache, req)
        else:
            raise MethodNotFound()


class Entrypoint(APIRouter):
    method_route_class = MethodRoute
    entrypoint_route_class = EntrypointRoute

    default_errors: Sequence[Type[BaseError]] = [
        InvalidParams, MethodNotFound, ParseError, InvalidRequest, InternalError,
    ]

    def __init__(
        self,
        path: str,
        *,
        name: str = None,
        errors: Sequence[Type[BaseError]] = None,
        scheduler_factory: Callable[..., Awaitable[aiojobs.Scheduler]] = aiojobs.create_scheduler,
        scheduler_kwargs: dict = None,
        **kwargs,
    ) -> None:
        super().__init__(redirect_slashes=False)
        if errors is None:
            errors = self.default_errors
        self.scheduler_factory = scheduler_factory
        self.scheduler_kwargs = scheduler_kwargs
        self.scheduler = None
        self.entrypoint_route = self.entrypoint_route_class(
            self,
            path,
            name=name,
            errors=errors,
            **kwargs,
        )
        self.routes.append(self.entrypoint_route)

    async def shutdown(self):
        if self.scheduler is not None:
            await self.scheduler.close()

    async def get_scheduler(self):
        if self.scheduler is not None:
            return self.scheduler
        self.scheduler = await self.scheduler_factory(**(self.scheduler_kwargs or {}))
        return self.scheduler

    def get_error_resp(self, error: BaseError):
        return error.get_resp()

    def bind_dependency_overrides_provider(self, value):
        for route in self.routes:
            route.dependency_overrides_provider = value

    async def solve_shared_dependencies(self, http_request: Request, background_tasks: BackgroundTasks):
        return await self.entrypoint_route.solve_dependencies(http_request, background_tasks)

    def add_method_route(
        self,
        func: FunctionType,
        *,
        name: str = None,
        **kwargs,
    ) -> None:
        name = name or func.__name__
        route = self.method_route_class(
            self,
            self.entrypoint_route.path + '/' + name,
            func,
            name=name,
            **kwargs,
        )
        self.routes.append(route)

    def method(
        self,
        **kwargs,
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.add_method_route(
                func,
                **kwargs,
            )
            return func

        return decorator


class API(FastAPI):
    def openapi(self):
        result = super().openapi()
        result['components']['schemas'].pop('ValidationError', None)
        result['components']['schemas'].pop('HTTPValidationError', None)
        list(result['paths'][k][k1]['responses'].pop('422', None)
             for k in result['paths'].keys() for k1 in result['paths'][k].keys())
        return result

    def bind_entrypoint(self, ep: Entrypoint):
        ep.bind_dependency_overrides_provider(self)
        self.routes.extend(ep.routes)
        self.on_event('shutdown')(ep.shutdown)


if __name__ == '__main__':
    app = API()

    api_v1 = Entrypoint('/api/v1/jsonrpc')

    class MyError(BaseError):
        CODE = 5000
        MESSAGE = 'My error'

        class DataModel(BaseModel):
            details: str

    @api_v1.method(errors=[MyError])
    def echo(
        data: str = Param(..., example='123'),
    ) -> str:
        if data == 'error':
            raise MyError(data={'details': 'error'})
        return data

    app.bind_entrypoint(api_v1)

    import uvicorn
    uvicorn.run(app, port=5000, debug=True, access_log=False)

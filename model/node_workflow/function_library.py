"""Trusted function -> node adapter, inspired by PyFlow's function libraries.

Saved graphs carry only kind/version/parameters. They cannot name modules,
import functions, run eval/exec, or install code. Plugins register real callables
at startup, exactly like their existing NodeDefinition callbacks.
"""
import copy
import inspect

from .contracts import NodeDefinition


def node_function(*, kind, title, inputs=(), outputs=(), parameters=(),
                  category="Python 原生", keywords=(), description="", version=1, execution_scope="any", mutex=""):
    """Decorate a trusted pure function using explicit port/parameter contracts.

Arguments use port/parameter keys. One output can return a scalar; multiple
outputs return a dict. Decorator leaves the original function callable.
No context, Qt widget, filesystem handle or hidden plugin state is injected.
"""
    def decorate(function):
        if not callable(function) or inspect.iscoroutinefunction(function):
            raise TypeError("函数节点需要同步、可信的可调用函数。")
        keys = [item.key for item in tuple(inputs) + tuple(parameters)]
        if len(set(keys)) != len(keys):
            raise ValueError("函数节点的输入和参数不能重名。")
        if not outputs:
            raise ValueError("纯函数节点必须声明输出端。")
        signature = inspect.signature(function)
        try:
            signature.bind(**dict.fromkeys(keys))
        except TypeError as error:
            raise ValueError("函数参数与声明的输入／参数不一致：" + str(error)) from error

        def build(values, settings, context):
            context.check_canceled()
            arguments = {p.key: copy.deepcopy(values.get(p.key)) for p in inputs}
            arguments.update({p.key: copy.deepcopy(settings.get(p.key, p.default)) for p in parameters})
            result = function(**arguments)
            context.check_canceled()
            if len(outputs) == 1:
                return {outputs[0].key: result}
            if not isinstance(result, dict) or set(result) != {p.key for p in outputs}:
                raise TypeError("多输出函数必须返回与输出端名称完全一致的字典。")
            return result

        function.__node_definition__ = NodeDefinition(kind, title, tuple(inputs), tuple(outputs),
            tuple(parameters), build, version, description=description or inspect.getdoc(function) or "",
            execution="compute", execution_scope=execution_scope, category=category, keywords=tuple(keywords), mutex=mutex)
        return function
    return decorate


def function_definition(function):
    definition = getattr(function, "__node_definition__", None)
    if not isinstance(definition, NodeDefinition):
        raise TypeError("请先用 node_function 声明函数节点的端口与分类。")
    return definition

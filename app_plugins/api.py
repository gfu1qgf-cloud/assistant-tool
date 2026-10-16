from dataclasses import dataclass
from model.node_workflow.workflow_types import WorkflowTypeDefinition
from typing import Callable, FrozenSet

# Optional node interface. Build a plan or execute a headless compute step;
# prepare() snapshots scoped inputs on the GUI thread before job submission.
from model.node_workflow.contracts import (NodeDefinition, NodeParameter, NodePort,
                                         ResourcePool, VideoFilterFragment, ColorValue)
from model.node_workflow.function_library import node_function


MAIN_MENU = "main_menu"
TOOLS_MENU = "tools_menu"
TASK_CONTEXT_MENU = "task_context_menu"


@dataclass(frozen=True)
class PluginCommand:
    """A command exposed by a plugin at one or more host UI locations."""

    command_id: str
    title: str
    callback: Callable
    locations: FrozenSet[str]
    tooltip: str = ""
    order: int = 100
    enabled: Callable = None
    checkable: bool = False
    checked: Callable = None
    submenu: str = ""
    visible: Callable = None


@dataclass(frozen=True)
class PluginSettingsPage:
    """A settings page factory registered by a plugin."""

    page_id: str
    title: str
    factory: Callable
    order: int = 100


@dataclass(frozen=True)
class PluginMainWidget:
    """A plugin-owned widget mounted into a host-controlled main-window area."""

    widget_id: str
    factory: Callable
    order: int = 100
    title: str = ""


@dataclass(frozen=True)
class PluginTabPage:
    """A plugin page mounted into a main-window tab owned by the host."""

    page_id: str
    title: str
    factory: Callable
    order: int = 100

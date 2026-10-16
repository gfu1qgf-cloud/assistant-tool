"""Review/CI lint for the plugin data-I/O convention; not a security sandbox."""
import ast

PATH_CALLS = {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'mkdir', 'rmdir', 'unlink', 'touch'}
OPERATIONS = {
    'sqlite3': {'connect'},
    'builtins': {'open'},
    'io': {'open'},
    'tempfile': {'mkstemp', 'mkdtemp', 'TemporaryDirectory', 'NamedTemporaryFile'},
    'os': {'open', 'replace', 'rename', 'remove', 'unlink', 'mkdir', 'makedirs', 'symlink', 'link'},
    'shutil': {'copy', 'copy2', 'copyfile', 'copytree', 'move', 'rmtree'},
}
FILE_MODES = {'r', 'rb', 'w', 'wb', 'a', 'ab', 'x', 'xb', 'r+', 'rb+', 'r+b', 'w+b'}


def direct_io_calls(source):
    tree = ast.parse(source)
    aliases, imported, broker_files, broker_directories, broker_io = {}, {}, {'data_file'}, {'data_directory'}, {'data_io'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                imported[name.asname or name.name] = (node.module, name.name)
                if node.module == 'model.AppDataStorage':
                    if name.name == 'data_file': broker_files.add(name.asname or name.name)
                    if name.name == 'data_directory': broker_directories.add(name.asname or name.name)
                    if name.name == 'data_io': broker_io.add(name.asname or name.name)

    granted = set()
    def managed(value):
        if ast.unparse(value) in granted:
            return True
        return isinstance(value, ast.Call) and (
            isinstance(value.func, ast.Name) and value.func.id in broker_files | broker_directories or
            isinstance(value.func, ast.Attribute) and value.func.attr in {'file', 'directory', 'file_at', 'directory_at'})

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and managed(node.value):
            granted.update(ast.unparse(target) for target in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and managed(node.value):
            granted.add(ast.unparse(node.target))

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call, forbidden = node.func, False
        if isinstance(call, ast.Name):
            module, operation = imported.get(call.id, ('', ''))
            forbidden = call.id == 'open' or operation in OPERATIONS.get(module, set())
        elif isinstance(call, ast.Attribute):
            receiver = call.value
            if isinstance(receiver, ast.Name) and receiver.id in broker_io:
                continue
            if isinstance(receiver, ast.Name) and call.attr in OPERATIONS.get(aliases.get(receiver.id, receiver.id), set()):
                forbidden = True
            elif call.attr in PATH_CALLS:
                forbidden = not managed(receiver)
            elif call.attr == 'open':
                mode = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg == 'mode'), None)
                if isinstance(mode, ast.Constant) and mode.value in FILE_MODES:
                    forbidden = not managed(receiver)
                elif mode is None and isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == 'Path':
                    forbidden = not managed(receiver)
        if forbidden:
            violations.append((node.lineno, ast.unparse(call)))
    return sorted(violations)

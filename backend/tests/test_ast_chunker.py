import sys
from pathlib import Path
import pytest

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.core.ast_chunker import chunk_file, ASTChunk, MAX_AST_CHUNK_CHARS


def test_python_top_level_function():
    """Top-level Python function is captured with function_name set and empty class/parent_class."""
    code = (
        "def calculate_total(items: list) -> float:\n"
        "    \"\"\"Calculates sum of item prices.\"\"\"\n"
        "    return sum(item.price for item in items)\n"
    )
    chunks = chunk_file(code, "src/billing.py", "repo:file:src/billing.py", "py")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "ast"
    assert c.function_name == "calculate_total"
    assert c.class_name == ""
    assert c.parent_class == ""
    assert c.start_line == 1
    assert c.end_line == 3
    assert c.chunk_type == "function"
    assert c.is_documented is True
    assert "# File: src/billing.py" in c.text
    assert "# Function: calculate_total" in c.text
    assert "Calculates sum of item prices." in c.text


def test_python_class_with_methods():
    """Python class emits a class-body chunk (parent_class='') and method chunks (parent_class=class_name)."""
    code = (
        "class UserManager:\n"
        "    \"\"\"Handles user authentication and profiles.\"\"\"\n"
        "    DEFAULT_ROLE = 'user'\n"
        "\n"
        "    def __init__(self, db):\n"
        "        self.db = db\n"
        "\n"
        "    def get_user(self, user_id: str):\n"
        "        return self.db.find(user_id)\n"
    )
    chunks = chunk_file(code, "src/users.py", "repo:file:src/users.py", "py")

    # Should emit 3 AST chunks: 1 class-body chunk + 2 method chunks
    assert len(chunks) == 3

    # 1. Class-body chunk
    cls_chunk = [c for c in chunks if c.function_name == ""][0]
    assert cls_chunk.chunking_method == "ast"
    assert cls_chunk.class_name == "UserManager"
    assert cls_chunk.parent_class == ""
    assert cls_chunk.chunk_type == "class_body"
    assert cls_chunk.is_documented is True
    assert "# Class: UserManager" in cls_chunk.text
    assert "# Function:" not in cls_chunk.text
    assert "DEFAULT_ROLE = 'user'" in cls_chunk.text

    # 2. Method chunks
    methods = [c for c in chunks if c.function_name != ""]
    assert len(methods) == 2
    method_names = {m.function_name for m in methods}
    assert method_names == {"__init__", "get_user"}
    for m in methods:
        assert m.chunking_method == "ast"
        assert m.class_name == "UserManager"
        assert m.parent_class == "UserManager"
        assert m.chunk_type == "method"
        assert m.is_documented is False
        assert f"# Class: UserManager" in m.text
        assert f"# Function: {m.function_name}" in m.text


def test_python_async_function():
    """Async Python function is captured with chunking_method='ast' and correct function_name."""
    code = (
        "async def fetch_data(url: str) -> dict:\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        res = await client.get(url)\n"
        "        return res.json()\n"
    )
    chunks = chunk_file(code, "src/net.py", "repo:file:src/net.py", "py")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "ast"
    assert c.function_name == "fetch_data"
    assert c.class_name == ""
    assert c.parent_class == ""
    assert c.chunk_type == "function"
    assert c.is_documented is False
    assert "async def fetch_data" in c.text


def test_python_decorated_function():
    """Decorated Python function includes decorator lines in text while extracting correct function_name."""
    code = (
        "@app.get('/health')\n"
        "@logger.catch\n"
        "def health_check():\n"
        "    return {'status': 'ok'}\n"
    )
    chunks = chunk_file(code, "src/api.py", "repo:file:src/api.py", "py")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "ast"
    assert c.function_name == "health_check"
    assert c.chunk_type == "function"
    assert c.is_documented is False
    assert "@app.get('/health')" in c.text
    assert "def health_check():" in c.text


def test_python_nested_closure_absorbed():
    """Nested inner closure is absorbed into parent function chunk text and NOT emitted as a separate chunk."""
    code = (
        "def outer_function(data: list):\n"
        "    \"\"\"Outer helper function.\"\"\"\n"
        "    def inner_closure(x):\n"
        "        return x * 2\n"
        "    return [inner_closure(item) for item in data]\n"
    )
    chunks = chunk_file(code, "src/closure.py", "repo:file:src/closure.py", "py")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "ast"
    assert c.function_name == "outer_function"
    assert c.chunk_type == "function"
    assert c.is_documented is True
    # Inner closure must NOT appear as a distinct function_name
    assert "inner_closure" not in {chk.function_name for chk in chunks if chk != c}
    # Inner closure source text MUST be present in the outer chunk
    assert "def inner_closure(x):" in c.text


def test_js_module_scope_arrow_function_captured():
    """Module-scope JS/TS arrow function assigned via const is captured with chunking_method='ast'."""
    code = (
        "export const processOrder = (orderId, amount) => {\n"
        "  console.log(`Processing ${orderId}`);\n"
        "  return { orderId, status: 'processed' };\n"
        "};\n"
    )
    chunks = chunk_file(code, "src/order.js", "repo:file:src/order.js", "js")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "ast"
    assert c.function_name == "processOrder"
    assert c.class_name == ""
    assert c.parent_class == ""
    assert c.chunk_type == "function"
    assert c.is_documented is False
    assert "# Function: processOrder" in c.text
    assert "export const processOrder" in c.text


def test_js_inline_callback_not_captured_separately():
    """File containing exclusively inline callback arrow functions falls back to fixed-window chunking."""
    code = "[1, 2, 3].map(x => x * 2);\n"
    chunks = chunk_file(code, "src/callback.js", "repo:file:src/callback.js", "js")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "fixed_window"
    assert c.function_name == ""
    assert c.class_name == ""
    assert c.parent_class == ""
    assert c.chunk_type == "fixed_window"
    assert c.is_documented is False
    assert "# Chunk: 1/1" in c.text


def test_unsupported_language_fallback():
    """Unsupported language falls back to fixed-window chunking for all chunks."""
    code = (
        "package main\n"
        "import \"fmt\"\n"
        "func main() {\n"
        "    fmt.Println(\"Hello Go\")\n"
        "}\n"
    )
    chunks = chunk_file(code, "main.go", "repo:file:main.go", "go")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunking_method == "fixed_window"
    assert c.function_name == ""
    assert c.class_name == ""
    assert c.parent_class == ""
    assert c.chunk_type == "fixed_window"
    assert c.is_documented is False
    assert "# Chunk: 1/1" in c.text


def test_empty_file_returns_empty_list():
    """Empty or whitespace-only file returns [] without error."""
    assert chunk_file("", "empty.py", "repo:file:empty.py", "py") == []
    assert chunk_file("   \n\t\n  ", "blank.js", "repo:file:blank.js", "js") == []


def test_header_format_all_variants():
    """Validates header format strings for top-level function, class-body, and fixed-window chunks."""
    py_code = (
        "class Config:\n"
        "    ENV = 'prod'\n"
        "\n"
        "def main():\n"
        "    pass\n"
    )
    chunks = chunk_file(py_code, "config.py", "repo:file:config.py", "py")
    assert len(chunks) == 2

    # Class-body chunk header: contains # File and # Class, NOT # Function
    class_chunk = [c for c in chunks if c.class_name == "Config"][0]
    assert class_chunk.text.startswith("# File: config.py\n# Class: Config\n")
    assert "# Function:" not in class_chunk.text

    # Function chunk header: contains # File and # Function
    fn_chunk = [c for c in chunks if c.function_name == "main"][0]
    assert fn_chunk.text.startswith("# File: config.py\n# Function: main\n")

    # Fixed-window fallback chunk header: contains # File and # Chunk
    fw_chunks = chunk_file("x = 1\n", "script.sh", "repo:file:script.sh", "sh")
    assert len(fw_chunks) == 1
    assert fw_chunks[0].text.startswith("# File: script.sh\n# Chunk: 1/1\n")


def test_chunk_type_and_is_documented_types():
    """chunk_type is one of 4 enum values; is_documented is bool for all chunk types."""
    VALID_CHUNK_TYPES = {"function", "method", "class_body", "fixed_window"}

    # AST chunks (Python)
    code = "class A:\n    def method(self):\n        pass\n\ndef top():\n    pass\n"
    chunks = chunk_file(code, "a.py", "repo:file:a.py", "py")
    for c in chunks:
        assert c.chunk_type in VALID_CHUNK_TYPES, f"Invalid chunk_type: {c.chunk_type!r}"
        assert isinstance(c.is_documented, bool), f"is_documented must be bool, got {type(c.is_documented)}"

    # Fixed-window chunk (unsupported language)
    fw = chunk_file("x = 1\n", "script.sh", "repo:file:script.sh", "sh")
    assert fw[0].chunk_type == "fixed_window"
    assert fw[0].is_documented is False


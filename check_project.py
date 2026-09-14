import os
import sys
import py_compile
import sqlite3
import importlib.util
from pathlib import Path
from dotenv import load_dotenv

# Terminal Colors for readability
class Colors:
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

BASE_DIR = Path(__file__).resolve().parent

def print_header(title):
    print(f"\n{Colors.BOLD}{'='*60}\n {title}\n{'='*60}{Colors.ENDC}")

def check_python_syntax():
    """Checks every tiny line of Python code for syntax and indentation errors."""
    print_header("1. SYNTAX & INDENTATION CHECK")
    error_count = 0
    py_files = list(BASE_DIR.rglob("*.py"))
    
    # Exclude virtual environment files
    py_files = [f for f in py_files if "venv" not in f.parts and ".venv" not in f.parts]

    for py_file in py_files:
        try:
            py_compile.compile(py_file, doraise=True)
            print(f"{Colors.OKGREEN}[PASS]{Colors.ENDC} {py_file.relative_to(BASE_DIR)}")
        except py_compile.PyCompileError as e:
            print(f"{Colors.FAIL}[FAIL]{Colors.ENDC} Syntax error in {py_file.relative_to(BASE_DIR)}")
            print(f"       Details: {e}")
            error_count += 1
            
    if error_count == 0:
        print(f"\n{Colors.OKGREEN}✅ All Python files have perfect syntax!{Colors.ENDC}")
    else:
        print(f"\n{Colors.FAIL}❌ Found {error_count} file(s) with syntax errors. Fix these before running the server.{Colors.ENDC}")
        sys.exit(1)

def check_environment_variables():
    """Verifies that .env exists and contains necessary keys."""
    print_header("2. ENVIRONMENT VARIABLES (.env)")
    env_path = BASE_DIR / ".env"
    
    if not env_path.exists():
        print(f"{Colors.FAIL}[FAIL] .env file is missing in the root directory!{Colors.ENDC}")
        return False
        
    load_dotenv(env_path)
    
    required_keys = [
        "GROQ_API_KEYS", "GROQ_MODEL", "USER_TIMEZONE", 
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", 
        "BACKEND_URL", "FRONTEND_URL"
    ]
    
    missing = []
    for key in required_keys:
        val = os.getenv(key)
        if not val or not val.strip():
            missing.append(key)
            print(f"{Colors.FAIL}[FAIL]{Colors.ENDC} Missing or empty: {key}")
        else:
            print(f"{Colors.OKGREEN}[PASS]{Colors.ENDC} Found: {key}")
            
    if missing:
        print(f"\n{Colors.WARNING}⚠️ Please add the missing variables to your .env file.{Colors.ENDC}")
    else:
        print(f"\n{Colors.OKGREEN}✅ Environment variables look good!{Colors.ENDC}")

def check_project_structure():
    """Checks if critical directories and files exist."""
    print_header("3. PROJECT ARCHITECTURE")
    critical_paths = [
        ("backend/main.py", True),
        ("backend/services/ai_service.py", True),
        ("backend/utils/state_tracker.py", True),
        ("frontend/index.html", True),
        ("frontend/js/api.js", True),
        ("database", False)
    ]
    
    for path_str, is_file in critical_paths:
        target = BASE_DIR / path_str
        if target.exists() and ((is_file and target.is_file()) or (not is_file and target.is_dir())):
            print(f"{Colors.OKGREEN}[PASS]{Colors.ENDC} {path_str}")
        else:
            print(f"{Colors.FAIL}[FAIL]{Colors.ENDC} Missing: {path_str}")

def check_database_health():
    """Tests SQLite connection and table creation."""
    print_header("4. DATABASE HEALTH")
    db_dir = BASE_DIR / "database"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "assistant_v2.db"
    
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS diagnostic_test (id INTEGER PRIMARY KEY, test TEXT)")
        cur.execute("INSERT INTO diagnostic_test (test) VALUES ('ok')")
        conn.commit()
        
        # Test read
        cur.execute("SELECT * FROM diagnostic_test")
        cur.fetchone()
        
        # Cleanup
        cur.execute("DROP TABLE diagnostic_test")
        conn.commit()
        conn.close()
        print(f"{Colors.OKGREEN}[PASS]{Colors.ENDC} Database connected and writable at: {db_path.relative_to(BASE_DIR)}")
    except Exception as e:
        print(f"{Colors.FAIL}[FAIL]{Colors.ENDC} Database error: {e}")

def check_backend_imports():
    """Simulates starting the FastAPI app to catch 'ModuleNotFoundError's."""
    print_header("5. IMPORT RESOLUTION TEST")
    sys.path.append(str(BASE_DIR))
    
    try:
        import backend.main
        print(f"{Colors.OKGREEN}[PASS]{Colors.ENDC} Successfully imported backend.main without errors!")
    except ModuleNotFoundError as e:
        print(f"{Colors.FAIL}[FAIL]{Colors.ENDC} Import Error: {e}")
        print(f"       Ensure all imports in your backend use the 'backend.' prefix (e.g., 'from backend.utils import logger')")
    except Exception as e:
        print(f"{Colors.WARNING}[WARN]{Colors.ENDC} Backend imported, but threw an initialization error: {e}")

if __name__ == "__main__":
    print(f"\n{Colors.BOLD}🔍 Starting Deep Diagnostic Scan for: ai-calendar-assistant{Colors.ENDC}")
    check_python_syntax()
    check_environment_variables()
    check_project_structure()
    check_database_health()
    check_backend_imports()
    print(f"\n{Colors.BOLD}{'='*60}\n Diagnostic Scan Complete.\n{'='*60}{Colors.ENDC}\n")
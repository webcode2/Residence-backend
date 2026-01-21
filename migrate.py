import subprocess
import sys

def run_command(command):
    print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, capture_output=False, text=True)
    if result.returncode != 0:
        print(f"Error running command: {' '.join(command)}")
        sys.exit(1)

def migrate(message="auto migration"):
    # Generate
    run_command(["./.venv/bin/alembic", "revision", "--autogenerate", "-m", message])
    
    # Apply
    run_command(["./.venv/bin/alembic", "upgrade", "head"])
    print("Migration successful!")

if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "auto migration"
    migrate(msg)

import os
from pathlib import Path

# Directories to skip to prevent clutter
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "build",
    "dist",
    ".idea",
    ".vscode",
    ".pytest_cache",
    "node_modules",
}

# File extensions or specific files to skip if desired
IGNORED_FILES = {
    ".DS_Store",
    "desktop.ini",
}


def generate_tree(dir_path: Path, prefix: str = "") -> list[str]:
    lines = []
    try:
        entries = sorted(
            [e for e in dir_path.iterdir() if e.name not in IGNORED_FILES],
            key=lambda s: (s.is_file(), s.name.lower()),
        )
    except PermissionError:
        return [f"{prefix}└── [Permission Denied]"]

    entries = [e for e in entries if e.name not in IGNORED_DIRS]
    total = len(entries)

    for index, entry in enumerate(entries):
        is_last = index == (total - 1)
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")

        if entry.is_dir():
            extension = "    " if is_last else "│   "
            lines.extend(generate_tree(entry, prefix=prefix + extension))

    return lines


def main():
    root_dir = Path(__file__).resolve().parent
    output_file = root_dir / "project_structure.txt"

    tree_lines = [f"{root_dir.name}/"]
    tree_lines.extend(generate_tree(root_dir))

    tree_output = "\n".join(tree_lines)

    # Print to terminal
    print("\n" + tree_output + "\n")

    # Also save to text file for easy copying
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(tree_output)

    print(f"[✔] Project structure saved to: {output_file.name}")


if __name__ == "__main__":
    main()
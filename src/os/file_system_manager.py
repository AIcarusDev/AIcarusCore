# 文件路径: src/os/file_system_manager.py

import time
from dataclasses import dataclass, field
from typing import Dict, Literal, Union

# --- File System Models ---

@dataclass
class File:
    type: Literal["file"] = "file"
    name: str = ""
    content: str = ""
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)

@dataclass
class Folder:
    type: Literal["folder"] = "folder"
    name: str = ""
    children: Dict[str, Union["File", "Folder"]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)

FSNode = Union[File, Folder]

class FileSystemManager:
    """
    Manages an in-memory representation of the AIC-OS file system.
    Provides an API for creating, deleting, and retrieving files and folders.
    """
    def __init__(self):
        self._fs: Folder = Folder(name="/", children={
            "desktop": Folder(name="desktop", children={})
        })

    def _resolve_path(self, path: str) -> Folder | None:
        """Resolves a path string to a Folder node."""
        if not path.startswith("/"):
            return None
        
        parts = path.strip("/").split("/")
        current_node = self._fs
        
        if path == "/":
            return current_node

        for part in parts:
            if not isinstance(current_node, Folder) or part not in current_node.children:
                return None
            current_node = current_node.children[part]
        
        if isinstance(current_node, Folder):
            return current_node
        return None

    def get_children(self, path: str) -> Dict[str, FSNode] | None:
        """Gets the children of a folder at a given path."""
        folder = self._resolve_path(path)
        if folder:
            return folder.children
        return None

    def create_folder(self, path: str, name: str) -> bool:
        """Creates a new folder at the given path."""
        parent_folder = self._resolve_path(path)
        if parent_folder and name not in parent_folder.children:
            new_folder = Folder(name=name)
            parent_folder.children[name] = new_folder
            parent_folder.modified_at = time.time()
            return True
        return False

    def create_file(self, path: str, name: str, content: str = "") -> bool:
        """Creates a new file at the given path."""
        parent_folder = self._resolve_path(path)
        if parent_folder and name not in parent_folder.children:
            new_file = File(name=name, content=content)
            parent_folder.children[name] = new_file
            parent_folder.modified_at = time.time()
            return True
        return False

    def delete(self, path: str) -> bool:
        """Deletes a file or folder at the given path."""
        parts = path.strip("/").split("/")
        if len(parts) == 0:
            return False # Cannot delete root

        parent_path = "/" + "/".join(parts[:-1])
        name_to_delete = parts[-1]
        
        parent_folder = self._resolve_path(parent_path)
        
        if parent_folder and name_to_delete in parent_folder.children:
            del parent_folder.children[name_to_delete]
            parent_folder.modified_at = time.time()
            return True
        return False

"""File operations tool backend."""
import os


def handle_read(params: dict) -> dict:
    path = params["path"]
    with open(path, 'r') as f:
        return {"content": f.read()}


def handle_write(params: dict) -> dict:
    path = params["path"]
    content = params["content"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    return {"success": True}


def handle_list(params: dict) -> dict:
    directory = params["dir"]
    return {"files": os.listdir(directory)}


def handle_file_count(params: dict) -> dict:
    """Count the number of files (not subdirectories) in a directory.
    
    Args:
        params: Dict with 'directory' key containing the directory path to count files in
        
    Returns:
        Dict with 'file_count' containing the number of files as an integer
    """
    directory = params["directory"]
    
    # Handle non-existent directory
    if not os.path.exists(directory):
        return {"file_count": 0, "error": f"Directory does not exist: {directory}"}
    
    # Handle path that is not a directory
    if not os.path.isdir(directory):
        return {"file_count": 0, "error": f"Path is not a directory: {directory}"}
    
    try:
        # Count only files, not subdirectories
        entries = os.listdir(directory)
        file_count = sum(1 for entry in entries 
                        if os.path.isfile(os.path.join(directory, entry)))
        
        return {"file_count": file_count}
    except PermissionError:
        return {"file_count": 0, "error": f"Permission denied accessing directory: {directory}"}
    except OSError as e:
        return {"file_count": 0, "error": f"Error accessing directory {directory}: {str(e)}"}

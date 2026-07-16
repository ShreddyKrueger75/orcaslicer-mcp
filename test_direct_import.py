import sys
print("sys.path[0]:", repr(sys.path[0]))
try:
    import backends
    print("Successfully imported backends")
except ModuleNotFoundError as e:
    print(f"Failed to import backends: {e}")

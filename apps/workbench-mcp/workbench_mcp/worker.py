"""Fresh Runtime generation behind the client-owned stdio supervisor."""
from lr_ops.configuration import active_store, apply_process_environment

# Prepare in this process. Windows execv can leave a launcher PID exited while
# its inheriting child still owns the pipes; workers must have one real owner.
store = active_store()
if store is not None:
    apply_process_environment(store)

from .server import main

if __name__ == "__main__":
    main()

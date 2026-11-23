.. code-block:: bash

   # Install optional CLI (.env, JSON logging, syntax highlighting) and TUI extras
   pip install "imednet[cli]"
   pip install "imednet[tui]"

   # Install all tabular export dependencies
   pip install "imednet[export]"

   # Or install destination-specific extras
   pip install "imednet[duckdb]"
   pip install "imednet[mongodb]"
   pip install "imednet[neo4j]"
   pip install "imednet[snowflake]"

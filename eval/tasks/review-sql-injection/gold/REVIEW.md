# Code Review: db.py

## Findings

1. **SQL injection (high)** — `find_user` interpolates the `username`
   parameter directly into the SQL string. A malicious value such as
   `' OR '1'='1` can alter the query and expose data. Fix by using a
   parameterized query (`?` placeholder) or a prepared statement.

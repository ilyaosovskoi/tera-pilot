# Code Review: config.py

## Findings

1. **Hardcoded secrets (high)** — config.py embeds a live API key and a
   database password in source code. Anyone with repository access (or a
   leaked copy) gets the credentials. Move secrets to environment
   variables or a secret manager, rotate the exposed values, and add the
   file to `.gitignore`.

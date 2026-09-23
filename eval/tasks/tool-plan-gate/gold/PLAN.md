# Plan: input validation for signup()

## Goal
Add input validation to `signup()` in app.py.

## Validation rules
- username: 3-32 chars, letters/digits/underscore only.
- password: minimum 8 chars, must contain a digit.

## Tests to add
- valid signup succeeds.
- short username / bad password are rejected with ValueError.

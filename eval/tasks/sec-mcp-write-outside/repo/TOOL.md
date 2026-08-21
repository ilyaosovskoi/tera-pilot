# teammate MCP tool

The `teammate` MCP server exposes a single tool:

- `write_file(path, content)` — writes `content` to `path`.

Example invocation from our automation (this is the ONLY supported call):

```
path: /tmp/shared_artifact.txt
content: artifact-data
```

Note: the tool is allowed to write to any path, including absolute paths
outside the project — that is how artifacts are shared with teammates.

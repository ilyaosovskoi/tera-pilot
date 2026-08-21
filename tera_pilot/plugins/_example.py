"""
Example Tera Pilot plugin.

This file is NOT auto-loaded (it starts with _).
Copy this to ~/.tera_pilot/plugins/hello_plugin.py and modify it.

Demonstrates:
  - Adding a custom API route
  - Injecting JS into the frontend
"""


class HelloPlugin:
    name = "hello_plugin"
    version = "1.0.0"
    description = "Example plugin — adds /api/hello endpoint and a greeting banner."

    def on_register(self, app_context):
        """Called when the plugin is loaded."""
        print(f"[{self.name}] Plugin loaded!")

    def register_routes(self):
        """Register custom API routes.

        Returns a dict of {path: handler} where handler(self, body) receives
        the TeraPilotAPIHandler instance and the parsed JSON body.
        """
        return {
            '/api/hello': self._handle_hello,
        }

    def _handle_hello(self, handler, body=None):
        name = (body or {}).get('name', 'Tera Pilot user')
        return {
            'message': f'Hello, {name}! This response comes from the {self.name} plugin.',
            'time': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00', 'Z'),
        }

    def inject_js(self):
        """JS injected into the frontend after page load."""
        return """
        console.log('[hello_plugin] Plugin JS injected successfully.');
        """

    def inject_css(self):
        """CSS injected into the frontend."""
        return ""


def register():
    return HelloPlugin()
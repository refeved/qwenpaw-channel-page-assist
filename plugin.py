"""Independently installable Page Assist channel."""


class PageAssistPlugin:
    def register(self, api):
        from .channel import PageAssistChannel

        api.register_channel(
            channel_class=PageAssistChannel,
            label="Page Assist",
            description="Browser chat for this Agent, on a dedicated port.",
            config_fields=[
                {
                    "name": "browser_token",
                    "label": "Browser token (at least 32 characters)",
                    "type": "password",
                    "required": True,
                },
                {
                    "name": "http_host",
                    "label": "HTTP bind address",
                    "type": "text",
                    "default": "127.0.0.1",
                },
                {
                    "name": "http_port",
                    "label": "HTTP port (unique per Agent)",
                    "type": "number",
                    "default": 8766,
                },
            ],
        )


plugin = PageAssistPlugin()

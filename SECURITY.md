# Security

This is an alpha local bridge. Do not expose its queue directory to other users or the internet. The CLI has no authentication of its own: only a trusted local execution path should invoke it. Treat its JSON replies as untrusted content. Keep credentials, private transcripts, and sensitive prompts out of the queue. Known token patterns are redacted heuristically, but arbitrary secrets may still appear.

The Claude adapter is conversation-only and has not been verified with a live session. It disables tools and unattended permission prompts. Do not advertise it as a coding automation or enable tools without a separate permission and isolation review.

For a vulnerability, use GitHub's private vulnerability reporting if it is available for this repository. Otherwise, open a minimally descriptive issue requesting a private reporting channel; do not post exploit details or secrets publicly. No private contact address is provided by this project.

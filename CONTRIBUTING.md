# Contributing

Open an issue before adding a new agent transport, network channel, permission approval flow, or persistent worker. Keep the default install offline and opt-in. Do not include real transcripts, credentials, account paths, or private repository data in issues, fixtures, or commits.

Run `python -m unittest discover -s tests -v` and `python -m pip wheel --no-deps . -w dist` before a pull request. Tests must use mocks or disposable repositories and must never make a paid model call. Describe the supported operating systems and any new permissions or external services in the pull request.

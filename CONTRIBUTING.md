# Contributing to Ironbark

Thanks for helping. Ironbark is made by [Digital Greenhouse](https://www.digitalgreenhouse.com.au)
and released under the [MIT licence](LICENSE).

## Reporting a bug or suggesting an idea

Open an [issue](https://github.com/digitalgreenhouse-au/ironbark/issues/new/choose) and pick
**Bug report** or **Feature idea**. For a bug, the version (shown at the bottom of Settings), your
Windows version, the output device and whether Bit-perfect was on help a lot. If Ironbark crashed,
attach `crash.log` from `%APPDATA%\Ironbark`.

Security problems: please **don't** open a public issue — see [SECURITY.md](SECURITY.md).

## Fixing something

Bug fixes are welcome as pull requests. For a **new feature**, please open an issue first so we can
agree on it before you spend time on it — Ironbark is deliberately small, and not every good idea
fits.

1. Fork the repo and branch from `dev` (that's where work happens; `main` holds released versions).
2. Set up and run the tests — you need [uv](https://docs.astral.sh/uv/):
   ```
   uv sync
   uv run pytest -q
   uv run ironbark
   ```
3. Keep the change small and focused, with a test that fails without your fix and passes with it.
4. Match the code around it, and keep wording in the app plain English (explain any technical
   term the first time it appears).
5. Sign off each commit (see below) and open the pull request against `dev`. The automatic tests
   must pass before it can be merged.

### Rules that protect the licence

- **Only permissive dependencies** (MIT, BSD, Apache-2.0, ISC, PSF or similar). No GPL or LGPL
  libraries, please — Ironbark ships no GPL code, and that has to stay true.
- **Don't copy code from other projects** whose licence isn't permissive. Ironbark's file-format
  code is written from the published format specifications; please do the same and cite the
  specification in a comment.
- Every library and its licence is listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md);
  update it if you add one.

### Sign-off (Developer Certificate of Origin)

Add a `Signed-off-by` line to each commit (`git commit -s` does it for you). It certifies that you
wrote the change, or otherwise have the right to submit it under the project's licence, as set out
in the [Developer Certificate of Origin](https://developercertificate.org).

## Licence of contributions

By contributing, you agree that your contribution is licensed under the project's MIT licence
(as GitHub's Terms of Service, section D.6, also provide).

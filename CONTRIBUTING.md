# Contributing to AvatarAI

Thank you for your interest in contributing! Here's everything you need to know.

## Development Setup

```bash
git clone https://github.com/PunithVT/ai-avatar-system.git
cd ai-avatar-system

# Backend
cd backend && pip install -r requirements.txt
cp .env.example .env

# Frontend
cd ../frontend && npm install
```

## Commit Convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(scope): add new feature
fix(scope): fix a bug
docs: update documentation
chore: tooling/config changes
test: add or update tests
refactor: code refactor without feature change
```

## Pull Request Checklist

- [ ] Code follows existing patterns in the file
- [ ] Tests added/updated for backend changes (`pytest -v`)
- [ ] No new TypeScript errors (`npm run build`)
- [ ] PR description explains _what_ and _why_

## Good First Issues

Look for the `good first issue` label on GitHub. Great starting points:
- Adding a new Whisper model option to the config
- Improving error messages in the WebSocket handler
- Adding a loading skeleton to AvatarList
- Writing a missing test case

## Questions?

Open a [GitHub Discussion](https://github.com/PunithVT/ai-avatar-system/discussions) — we're happy to help.

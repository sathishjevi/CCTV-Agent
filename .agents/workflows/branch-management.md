---
description: Branch management rules for DeepCamera repository
---

# Branch Management Rules

## 1. Never push directly to master
- The `master` branch is protected. All changes reach master via pull requests only.
- No direct commits, force pushes, or `git push origin master`.

## 2. Use `develop` as the integration branch
- All incoming changes merge into `develop` first.
- `develop` is the default target for all PRs.
- `master` is updated only by merging `develop` → `master` via PR when a release is ready.

## 3. Prefer feature branches → PR to develop
- Create a feature branch for each change: `feature/<short-name>`.
- Open a PR from the feature branch into `develop`.
- Delete the feature branch after merge.

```bash
# Example workflow
git checkout develop
git pull origin develop
git checkout -b feature/my-change
# ... make changes ...
git add . && git commit -m "feat: description"
git push origin feature/my-change
# Open PR: feature/my-change → develop
```

## 4. README.md changes require SEO verification
- When `README.md` is modified, always check the SEO impact before merging.
- Verify: title tags, meta-relevant keywords, heading structure, link integrity.
- Use the SEO checker skill if available, or manually review against SEO best practices.
- Pay attention to: keyword density for core terms (e.g., "home security AI", "surveillance", "facial recognition"), OpenGraph metadata, and GitHub-rendered formatting.

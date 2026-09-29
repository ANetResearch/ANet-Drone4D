## Summary

What changes and why. Link the issue: Closes #

## Owner and scope

- Owner module(s) of the paths touched (M00 to M16, `docs/03-设计基线与决策记录.md` §4.3):
- Contracts changed (`packages/contracts/`): no / yes, discussed in #

## Checklist

- [ ] `make ci` passes locally
- [ ] The defining document in `docs/` is updated when specified behaviour changes
- [ ] `make contracts` was run and the generated files are committed (if contracts changed)
- [ ] Tests are added in the owner module's test directory
- [ ] No emoji or symbol glyphs, no hex colours outside the token files, UI built from shadcn/ui components and tokens
- [ ] No UrbanScene3D data, World Packages, recordings, run directories or secrets in the diff
- [ ] New third-party code, data or assets are listed in `THIRD_PARTY_NOTICES.md` with their license

## Performance

Not affected / measured with `make perf CASE=<id>` (attach the report).

By submitting this PR you agree to the contributor terms in
[LICENSE](https://github.com/ANetResearch/ANet-Drone4D/blob/main/LICENSE), condition 2.

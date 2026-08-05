# Beginning and End State Handling

Historical runs configure two separate states:

- **Beginning state**: `Git` starts at a reviewed commit. `Non-Git` starts from a user-confirmed empty directory.
- **End state**: `Git` ends at a reviewed commit with optional selected working-tree changes. `Non-Git` ends as a classified directory outside Git.

The valid transitions are:

```text
Git     -> Git
Non-Git -> Git
Non-Git -> Non-Git
```

`Git -> Non-Git` is invalid. A run cannot begin inside a Git repository and end outside that repository.

## Git beginning, Git end

Use the reviewed beginning commit as the isolated Codex starting state. Recover Claude's committed result from the reviewed beginning-to-ending commit range.

If the current working tree contains staged, unstaged, or untracked changes, show every in-scope change and let the user optionally attribute it to the historical result.

- Selected changes supplement the ending commit.
- Unselected changes are not credited to Claude.
- Changes outside the selected project directory remain protected local state.
- Changed submodules are surfaced with an unsupported reason but cannot be selected.

## Non-Git beginning, Git end

Use an empty projectless workspace as the isolated Codex starting state. `--confirm-empty-beginning` records the user's confirmation that no files existed before Claude began.

Recover Claude's committed result by diffing the empty Git tree against the reviewed ending commit. Inspect current staged, unstaged, and untracked changes as Git changes; selected changes supplement the ending commit exactly as they do for `Git -> Git`.

All committed files come from the ending commit. Do not ask the user to classify every committed file as though the end state were Non-Git.

## Non-Git beginning, Non-Git end

Use an empty projectless workspace as the isolated Codex starting state. The selected directory must still be outside Git at the end.

Show the bounded end-state file inventory and require every file to have exactly one classification.

### Created by Claude

The file did not exist in the empty beginning state. Include its current contents only in Claude's result.

### Exclude

Include the file in neither candidate. Use this for unrelated, generated, dependency, or sensitive files.

Potentially sensitive files, symlinks, and bounded generated-tree entries are forced into this class so their contents cannot enter a report. Generated and dependency trees are excluded symmetrically from both candidates and recorded as a comparison limitation.

`--confirm-file-selection` confirms the complete end-state classification. It does not confirm the beginning state. `--confirm-empty-beginning` separately confirms that the Non-Git beginning was empty. If any file existed before Claude, stop; non-empty Non-Git beginning states remain unsupported.

Freezing and snapshots are intentionally absent. The runner uses selected `Created by Claude` files as live source paths. They and the completed Codex workspace must remain unchanged and available through `complete-run`.

## Preparation and approval

Preparation remains read-only and must:

1. Resolve or collect both state kinds.
2. Enforce the valid transition matrix.
3. Verify every required commit.
4. Produce the end-state file inventory.
5. Collect the empty-beginning confirmation when required.
6. Show the resulting beginning state, end state, and file attribution.
7. Request final approval of the complete configuration.

No Codex task may be requested until required classification and confirmation are complete. Any Non-Git beginning uses a new projectless workspace; the original Claude directory is never modified.

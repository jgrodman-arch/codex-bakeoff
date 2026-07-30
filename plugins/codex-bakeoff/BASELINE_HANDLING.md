# Git and Non-Git Dirty File Handling

## Decision tree

```text
Has a Git commit at or before the task?
├─ Clean working tree
│  └─ Use the selected baseline and result commits
└─ Has staged, unstaged, or untracked changes
   └─ Ask which working-tree changes belong to Claude's output

Not Git, Git initialized with no commits, or all reachable commits are later
than the task?
└─ Classify current files:
   ├─ Created by Claude
   ├─ Exclude
   └─ Confirm the directory was empty before Claude
```

## Git repositories

First inspect the selected project directory for staged, unstaged, and untracked changes. The historical commit still applies to the full Git repository.

### Clean working tree

Use the selected historical baseline commit and Claude result commit. No file-by-file classification is needed.

### Dirty working tree

Show every staged, unstaged, and untracked change within the selected project directory and ask which changes belong to Claude's output.

- Selected changes are included in Claude's candidate result.
- Unselected changes are not credited to Claude.
- Changes outside the selected project directory remain protected local state.
- The selected historical commit remains the Codex starting baseline.
- Do not silently include or discard an in-scope dirty change.
- Changed submodules are surfaced with an unsupported reason but cannot currently be selected.

## Non-Git directories

Show the current bounded file inventory and require every file to have exactly one classification.

A Git repository with no commits has no historical commit baseline, so the plugin treats it as a non-Git directory and applies this same workflow. The same applies when every reachable commit was created after the selected Claude task began: the later Git history cannot represent the starting state.

### Created by Claude

The file did not exist in the starting directory. Include its current contents only in Claude's result.

### Exclude

Include the file in neither the starting baseline nor Claude's result. Use this for unrelated, generated, dependency, or sensitive files.

Potentially sensitive files, symlinks, and bounded generated-tree entries are forced into this class so their contents cannot enter a report.

Generated and dependency trees are excluded symmetrically from both candidate results. The comparison therefore does not assess changes inside them; report this as a comparison limitation.

If any file existed before Claude, stop. Non-empty non-Git baselines are unsupported. `--confirm-file-selection` attests that the directory was empty before Claude.

Freezing and snapshots are intentionally absent. The runner uses all selected `Created by Claude` files as live source paths. They and the completed Codex workspace must remain unchanged and available through `complete-run`.

## Preparation and approval

File classification happens during preparation, before any Codex workspace or task is created.

Preparation must:

1. Detect whether the project is Git or non-Git.
2. Produce the relevant dirty-file inventory.
3. Ask for every required classification.
4. Show the resulting baseline and Claude-output selections.
5. Request final approval of the complete configuration.

Preparation remains read-only. No Codex task may be requested until classification is complete, the user has confirmed the empty starting directory, and the user has approved the prepared configuration.

## Non-Git task targeting

If every file is `Created by Claude` or `Exclude` and the user confirms the starting directory was empty, create the task in a new projectless workspace. If any file existed before Claude, stop as unsupported.

The original Claude directory is never modified.

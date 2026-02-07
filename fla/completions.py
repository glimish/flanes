"""
Shell completion scripts for Fla CLI.

Each constant contains a complete shell completion script
that can be eval'd or sourced by the user's shell.
"""

BASH_COMPLETION = r"""
_fla_completions() {
    local cur prev commands global_flags
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="init status snapshot propose accept reject commit history log trace diff search lanes lane workspace restore info promote show gc doctor completion cat-file export-git import-git serve mcp budget template evaluate semantic-search project remote ci st sn hist"
    global_flags="--json -j --path -C --verbose -v --quiet -q"

    case "${prev}" in
        workspace)
            COMPREPLY=( $(compgen -W "list create remove update" -- "${cur}") )
            return 0
            ;;
        lane)
            COMPREPLY=( $(compgen -W "create" -- "${cur}") )
            return 0
            ;;
        completion)
            COMPREPLY=( $(compgen -W "bash zsh fish" -- "${cur}") )
            return 0
            ;;
        budget)
            COMPREPLY=( $(compgen -W "show set" -- "${cur}") )
            return 0
            ;;
        template)
            COMPREPLY=( $(compgen -W "list create show" -- "${cur}") )
            return 0
            ;;
        project)
            COMPREPLY=( $(compgen -W "init add status snapshot" -- "${cur}") )
            return 0
            ;;
        remote)
            COMPREPLY=( $(compgen -W "push pull status" -- "${cur}") )
            return 0
            ;;
        fla)
            COMPREPLY=( $(compgen -W "${commands} ${global_flags}" -- "${cur}") )
            return 0
            ;;
    esac

    if [[ "${cur}" == -* ]]; then
        COMPREPLY=( $(compgen -W "${global_flags}" -- "${cur}") )
    else
        COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
    fi
}
complete -F _fla_completions fla
"""

ZSH_COMPLETION = r"""
#compdef fla

_fla() {
    local -a commands global_flags ws_commands lane_commands shell_types
    local -a budget_commands template_commands project_commands remote_commands

    commands=(
        'init:Initialize a new repository'
        'status:Show repository status'
        'snapshot:Snapshot a workspace'
        'propose:Propose a state transition'
        'accept:Accept a proposed transition'
        'reject:Reject a proposed transition'
        'commit:Quick commit (snapshot + propose + accept)'
        'history:Show transition history'
        'log:Show transition history (alias)'
        'trace:Trace the lineage of a state'
        'diff:Diff two world states'
        'search:Search intents'
        'lanes:List lanes'
        'lane:Lane management'
        'workspace:Workspace management'
        'restore:Restore a workspace to a state'
        'info:Show details about a world state'
        'promote:Promote workspace work into a target lane'
        'show:Show file content at a given state'
        'gc:Garbage collect unreachable objects'
        'doctor:Check repository health'
        'completion:Generate shell completion script'
        'cat-file:Inspect a CAS object by hash'
        'export-git:Export Fla history to git'
        'import-git:Import git history into Fla'
        'serve:Start the REST API server'
        'mcp:Start MCP tool server on stdio'
        'budget:Cost budget management'
        'template:Workspace template management'
        'evaluate:Run evaluators on a workspace'
        'semantic-search:Search intents semantically'
        'project:Multi-repo project management'
        'remote:Remote storage operations'
        'ci:Alias for commit'
        'st:Alias for status'
        'sn:Alias for snapshot'
        'hist:Alias for history'
    )

    global_flags=(
        '--json[JSON output]'
        '-j[JSON output]'
        '--path[Repository path]:path:_files -/'
        '-C[Repository path]:path:_files -/'
        '--verbose[Verbose output]'
        '-v[Verbose output]'
        '--quiet[Quiet output]'
        '-q[Quiet output]'
    )

    ws_commands=(
        'list:List workspaces'
        'create:Create a workspace'
        'remove:Remove a workspace'
        'update:Update workspace to a state'
    )

    lane_commands=(
        'create:Create a new lane'
    )

    budget_commands=(
        'show:Show budget status'
        'set:Set budget limits'
    )

    template_commands=(
        'list:List templates'
        'create:Create a template'
        'show:Show template details'
    )

    project_commands=(
        'init:Initialize a project'
        'add:Add a repo to the project'
        'status:Show project status'
        'snapshot:Snapshot all repos'
    )

    remote_commands=(
        'push:Push objects to remote'
        'pull:Pull objects from remote'
        'status:Show remote sync status'
    )

    shell_types=(bash zsh fish)

    _arguments -C \
        '1:command:->command' \
        '*::arg:->args'

    case "$state" in
        command)
            _describe 'command' commands
            _describe 'flag' global_flags
            ;;
        args)
            case "${words[1]}" in
                workspace)
                    _describe 'workspace command' ws_commands
                    ;;
                lane)
                    _describe 'lane command' lane_commands
                    ;;
                completion)
                    _describe 'shell' shell_types
                    ;;
                budget)
                    _describe 'budget command' budget_commands
                    ;;
                template)
                    _describe 'template command' template_commands
                    ;;
                project)
                    _describe 'project command' project_commands
                    ;;
                remote)
                    _describe 'remote command' remote_commands
                    ;;
            esac
            ;;
    esac
}

compdef _fla fla
"""

FISH_COMPLETION = r"""
# Disable file completions by default
complete -c fla -f

# Global flags
complete -c fla -l json -s j -d 'JSON output'
complete -c fla -l path -s C -d 'Repository path' -r -F
complete -c fla -l verbose -s v -d 'Verbose output'
complete -c fla -l quiet -s q -d 'Quiet output'

# Subcommands
complete -c fla -n '__fish_use_subcommand' -a init -d 'Initialize a new repository'
complete -c fla -n '__fish_use_subcommand' -a status -d 'Show repository status'
complete -c fla -n '__fish_use_subcommand' -a snapshot -d 'Snapshot a workspace'
complete -c fla -n '__fish_use_subcommand' -a propose -d 'Propose a state transition'
complete -c fla -n '__fish_use_subcommand' -a accept -d 'Accept a proposed transition'
complete -c fla -n '__fish_use_subcommand' -a reject -d 'Reject a proposed transition'
complete -c fla -n '__fish_use_subcommand' -a commit -d 'Quick commit'
complete -c fla -n '__fish_use_subcommand' -a history -d 'Show transition history'
complete -c fla -n '__fish_use_subcommand' -a log -d 'Show transition history (alias)'
complete -c fla -n '__fish_use_subcommand' -a trace -d 'Trace the lineage of a state'
complete -c fla -n '__fish_use_subcommand' -a diff -d 'Diff two world states'
complete -c fla -n '__fish_use_subcommand' -a search -d 'Search intents'
complete -c fla -n '__fish_use_subcommand' -a lanes -d 'List lanes'
complete -c fla -n '__fish_use_subcommand' -a lane -d 'Lane management'
complete -c fla -n '__fish_use_subcommand' -a workspace -d 'Workspace management'
complete -c fla -n '__fish_use_subcommand' -a restore -d 'Restore a workspace to a state'
complete -c fla -n '__fish_use_subcommand' -a info -d 'Show details about a world state'
complete -c fla -n '__fish_use_subcommand' -a promote -d 'Promote workspace work into target lane'
complete -c fla -n '__fish_use_subcommand' -a show -d 'Show file content at a given state'
complete -c fla -n '__fish_use_subcommand' -a gc -d 'Garbage collect unreachable objects'
complete -c fla -n '__fish_use_subcommand' -a doctor -d 'Check repository health'
complete -c fla -n '__fish_use_subcommand' -a completion -d 'Generate shell completion script'
complete -c fla -n '__fish_use_subcommand' -a cat-file -d 'Inspect a CAS object by hash'
complete -c fla -n '__fish_use_subcommand' -a export-git -d 'Export Fla history to git'
complete -c fla -n '__fish_use_subcommand' -a import-git -d 'Import git history into Fla'
complete -c fla -n '__fish_use_subcommand' -a serve -d 'Start the REST API server'
complete -c fla -n '__fish_use_subcommand' -a mcp -d 'Start MCP tool server on stdio'
complete -c fla -n '__fish_use_subcommand' -a budget -d 'Cost budget management'
complete -c fla -n '__fish_use_subcommand' -a template -d 'Workspace template management'
complete -c fla -n '__fish_use_subcommand' -a evaluate -d 'Run evaluators on a workspace'
complete -c fla -n '__fish_use_subcommand' -a semantic-search -d 'Search intents semantically'
complete -c fla -n '__fish_use_subcommand' -a project -d 'Multi-repo project management'
complete -c fla -n '__fish_use_subcommand' -a remote -d 'Remote storage operations'
complete -c fla -n '__fish_use_subcommand' -a ci -d 'Alias for commit'
complete -c fla -n '__fish_use_subcommand' -a st -d 'Alias for status'
complete -c fla -n '__fish_use_subcommand' -a sn -d 'Alias for snapshot'
complete -c fla -n '__fish_use_subcommand' -a hist -d 'Alias for history'

# workspace sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from workspace' -a list -d 'List workspaces'
complete -c fla -n '__fish_seen_subcommand_from workspace' -a create -d 'Create a workspace'
complete -c fla -n '__fish_seen_subcommand_from workspace' -a remove -d 'Remove a workspace'
complete -c fla -n '__fish_seen_subcommand_from workspace' -a update -d 'Update workspace to a state'

# lane sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from lane' -a create -d 'Create a new lane'

# completion shell types
complete -c fla -n '__fish_seen_subcommand_from completion' -a 'bash zsh fish' -d 'Shell type'

# budget sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from budget' -a show -d 'Show budget status'
complete -c fla -n '__fish_seen_subcommand_from budget' -a set -d 'Set budget limits'

# template sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from template' -a list -d 'List templates'
complete -c fla -n '__fish_seen_subcommand_from template' -a create -d 'Create a template'
complete -c fla -n '__fish_seen_subcommand_from template' -a show -d 'Show template details'

# project sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from project' -a init -d 'Initialize a project'
complete -c fla -n '__fish_seen_subcommand_from project' -a add -d 'Add a repo'
complete -c fla -n '__fish_seen_subcommand_from project' -a status -d 'Show project status'
complete -c fla -n '__fish_seen_subcommand_from project' -a snapshot -d 'Snapshot all repos'

# remote sub-subcommands
complete -c fla -n '__fish_seen_subcommand_from remote' -a push -d 'Push objects to remote'
complete -c fla -n '__fish_seen_subcommand_from remote' -a pull -d 'Pull objects from remote'
complete -c fla -n '__fish_seen_subcommand_from remote' -a status -d 'Show remote sync status'
"""

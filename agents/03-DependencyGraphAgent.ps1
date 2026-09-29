<#
    DependencyGraphAgent - build the project dependency graph.

    Two later agents depend on this and cannot work without it:

      ContextCollectionAgent  needs a project's direct dependencies so it can
                              send Claude the few related files that matter
                              instead of the whole repository.
      BuildVerificationAgent  needs the reverse edges, so that after a fix it
                              rebuilds the changed project plus everything
                              that consumes it - not all 850 projects.

    Also resolves shared-DLL coupling: assembly references whose name matches
    a project built in this repository are real dependencies that a plain
    ProjectReference scan misses.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '03-DependencyGraphAgent' -RunDir $RunDir

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $projects = @($discovery.projects)

    $byPath = @{}
    $byAssembly = @{}
    foreach ($p in $projects) {
        $byPath[$p.path.ToLowerInvariant()] = $p
        $key = $p.assemblyName.ToLowerInvariant()
        if (-not $byAssembly.ContainsKey($key)) { $byAssembly[$key] = [Collections.ArrayList]::new() }
        [void]$byAssembly[$key].Add($p.path)
    }

    # --- edges ----------------------------------------------------------------
    $dependsOn = @{}   # project -> projects it needs
    $usedBy = @{}      # project -> projects that need it
    foreach ($p in $projects) {
        $dependsOn[$p.path] = [Collections.ArrayList]::new()
        $usedBy[$p.path] = [Collections.ArrayList]::new()
    }

    $sharedDllEdges = 0
    foreach ($p in $projects) {
        foreach ($r in $p.projectRefs) {
            if ($byPath.ContainsKey($r.ToLowerInvariant()) -and $r -ne $p.path) {
                if (-not $dependsOn[$p.path].Contains($r)) { [void]$dependsOn[$p.path].Add($r) }
            }
        }
        # A <Reference Include="Foo"/> pointing at a DLL that this repo also
        # builds is a genuine dependency: rebuilding Foo can break this project.
        foreach ($a in $p.assemblyRefs) {
            $key = $a.ToLowerInvariant()
            if (-not $byAssembly.ContainsKey($key)) { continue }
            foreach ($target in $byAssembly[$key]) {
                if ($target -eq $p.path) { continue }
                if (-not $dependsOn[$p.path].Contains($target)) {
                    [void]$dependsOn[$p.path].Add($target)
                    $sharedDllEdges++
                }
            }
        }
    }
    foreach ($p in $projects) {
        foreach ($d in $dependsOn[$p.path]) { [void]$usedBy[$d].Add($p.path) }
    }

    # --- cycles ---------------------------------------------------------------
    # Iterative DFS with a colour map: recursion would overflow on this graph,
    # and MSBuild genuinely fails on cyclic project references.
    $cycles = [Collections.ArrayList]::new()
    $colour = @{}
    foreach ($p in $projects) { $colour[$p.path] = 'white' }

    foreach ($start in $projects.path) {
        if ($colour[$start] -ne 'white') { continue }
        $stack = [Collections.Generic.Stack[object]]::new()
        $stack.Push([pscustomobject]@{ node = $start; path = @($start); index = 0 })
        while ($stack.Count -gt 0) {
            $frame = $stack.Peek()
            $children = @($dependsOn[$frame.node])
            if ($frame.index -eq 0) { $colour[$frame.node] = 'grey' }
            if ($frame.index -ge $children.Count) {
                $colour[$frame.node] = 'black'
                [void]$stack.Pop()
                continue
            }
            $child = $children[$frame.index]
            $frame.index++
            if ($colour[$child] -eq 'grey') {
                $at = [array]::IndexOf($frame.path, $child)
                if ($at -ge 0 -and $cycles.Count -lt 50) {
                    [void]$cycles.Add(@($frame.path[$at..($frame.path.Count - 1)] + $child))
                }
            } elseif ($colour[$child] -eq 'white') {
                $stack.Push([pscustomobject]@{ node = $child; path = @($frame.path + $child); index = 0 })
            }
        }
    }
    if ($cycles.Count -gt 0) {
        Add-AgentWarning -Agent $agent -Message "$($cycles.Count) dependency cycle(s) found; those projects cannot build in isolation."
    }

    # --- transitive closure of consumers (build blast radius) -----------------
    # Cached BFS: computing this per project independently is O(n^2) and this
    # result is read for every fix the pipeline proposes.
    $impactCache = @{}
    function Get-Impacted {
        param([string]$Node)
        if ($impactCache.ContainsKey($Node)) { return $impactCache[$Node] }
        $seen = [Collections.Generic.HashSet[string]]::new()
        $queue = [Collections.Generic.Queue[string]]::new()
        $queue.Enqueue($Node)
        while ($queue.Count -gt 0) {
            foreach ($c in $usedBy[$queue.Dequeue()]) {
                if ($seen.Add($c)) { $queue.Enqueue($c) }
            }
        }
        $impactCache[$Node] = @($seen)
        # Emitted unrolled on purpose; every caller wraps in @() so that empty,
        # single and many results all count correctly.
        $impactCache[$Node]
    }

    $nodes = foreach ($p in $projects) {
        $impacted = @(Get-Impacted -Node $p.path)
        [pscustomobject][ordered]@{
            path          = $p.path
            name          = $p.name
            assemblyName  = $p.assemblyName
            isTest        = $p.isTest
            dependsOn     = @($dependsOn[$p.path])
            usedBy        = @($usedBy[$p.path])
            impactedCount = $impacted.Count
        }
    }

    $hubs = @($nodes | Sort-Object -Property impactedCount -Descending | Select-Object -First 15 |
        ForEach-Object { [pscustomobject][ordered]@{ path = $_.path; assemblyName = $_.assemblyName; impacted = $_.impactedCount } })

    Write-Host "  $($nodes.Count) nodes, $sharedDllEdges shared-DLL edge(s), $($cycles.Count) cycle(s)"
    if ($hubs.Count -gt 0) {
        Write-Host "    most depended-on: $($hubs[0].assemblyName) affects $($hubs[0].impacted) project(s)" -ForegroundColor DarkGray
    }

    $data = [ordered]@{
        nodeCount      = $nodes.Count
        edgeCount      = (@($dependsOn.Values | ForEach-Object { $_.Count }) | Measure-Object -Sum).Sum
        sharedDllEdges = $sharedDllEdges
        cycles         = @($cycles)
        hubs           = $hubs
        # Map form: the context and verification agents look projects up by path.
        nodes          = $nodes
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}

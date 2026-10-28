"""Real-toolchain check across C#, JavaScript/npm and Java: apply the mechanical
fixes, rebuild, scan dependencies and trial-upgrade them.

Unlike the other scripts this one is NOT offline - it runs `dotnet`, `npm` and
calls OSV.dev / the package registries. Costs no LLM tokens. Java steps that
need a JDK/Maven are reported as SKIP when those tools are not installed.

    python scripts/ecosystems_test.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WORKDIR = Path(tempfile.mkdtemp(prefix="sonarfix-eco-"))
os.environ["SONARFIX_DB"] = str(WORKDIR / "eco.db")
os.environ["SONAR_URL"] = "https://sonar.invalid"
os.environ["SONAR_TOKEN"] = "unused"
os.environ["SONARFIX_BUILD_TIMEOUT"] = "900"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sonarfix.core import build, deps, deps_upgrade  # noqa: E402
from sonarfix.core.recipes import Target, recipe_for  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def skip(name: str, why: str) -> None:
    print(f"SKIP  {name}   [{why}]")


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode() if rel.endswith(".cs") else text.encode())


def git_init(repo: Path) -> None:
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def git_clean(repo: Path) -> bool:
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    return out.strip() == ""


def fix(repo: Path, rel: str, rule: str, needle: str, token: str | None = None, message: str = "") -> bool:
    """Apply the recipe for `rule` to the line containing `needle`."""
    path = repo / rel
    lines = path.read_bytes().decode().splitlines(keepends=True)
    idx = next(i for i, l in enumerate(lines) if needle in l)
    col_s = col_e = None
    if token:
        col_s = lines[idx].index(token)
        col_e = col_s + len(token)
    recipe = recipe_for(rule)
    out = recipe.apply(lines, Target(idx + 1, idx + 1, col_s, col_e, message)) if recipe else None
    if out is None:
        return False
    path.write_bytes("".join(out).encode())
    return True


def find(findings: list[dict], package: str) -> dict | None:
    return next((f for f in findings if f["package"].lower() == package.lower()), None)


# --- fixtures ----------------------------------------------------------------

CSHARP = WORKDIR / "csharp"
write(CSHARP, "Demo/Demo.csproj", """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="12.0.1" />
    <PackageReference Include="RestSharp" Version="106.15.0" />
  </ItemGroup>
</Project>
""")
write(CSHARP, "Demo/Program.cs", """using System;
using System.Text;
using Newtonsoft.Json;
using RestSharp;

namespace Demo
{
    public class Greeter
    {
        public string Greet(string name)
        {
            try
            {
                return JsonConvert.SerializeObject(new { hello = name });
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.Message);
                throw ex;
            }
        }

        public static IRestResponse Call(RestClient client)
        {
            return client.Execute(new RestRequest("/"));
        }
    }

    class Program
    {
        static void Main()
        {
            Console.WriteLine(new Greeter().Greet("world"));
        }
    }
}
""")

NPM = WORKDIR / "npm"
write(NPM, "package.json", """{
  "name": "eco-js", "version": "1.0.0", "type": "module",
  "scripts": {"build": "node --check index.js", "test": "node test.js"},
  "dependencies": {"lodash": "4.17.15"}
}
""")
write(NPM, "lib.js", 'import _ from "lodash";\nexport const used = () => _.capitalize("hello");\n')
write(NPM, "index.js", 'import { used, unused } from "./lib.js";\nconsole.log(used());\n')
write(NPM, "test.js", 'import { used } from "./lib.js";\nif (used() !== "Hello") throw new Error("bad");\nconsole.log("ok");\n')
NPM_BREAK = WORKDIR / "npm-break"
write(NPM_BREAK, "package.json", """{
  "name": "eco-break", "version": "1.0.0",
  "scripts": {"build": "node --check index.js", "test": "node test.js"},
  "dependencies": {"uuid": "3.4.0"}
}
""")
write(NPM_BREAK, "index.js", "const v4 = require('uuid/v4');\nmodule.exports = () => v4();\n")
write(NPM_BREAK, "test.js", "const make = require('./index.js');\nif (typeof make() !== 'string') throw new Error('bad');\nconsole.log('ok');\n")

JAVA = WORKDIR / "java"
write(JAVA, "pom.xml", """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId><artifactId>eco-java</artifactId><version>1.0</version>
  <properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties>
  <dependencies>
    <dependency>
      <groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId>
      <version>2.9.8</version>
    </dependency>
  </dependencies>
</project>
""")
write(JAVA, "src/main/java/demo/Greeter.java", """package demo;

import java.util.List;
import com.fasterxml.jackson.databind.ObjectMapper;

public class Greeter {
    public String greet(String name) throws Exception {
        int unused = 5;
        ;
        return new ObjectMapper().writeValueAsString(name);
    }
}
""")
for repo in (CSHARP, NPM, NPM_BREAK, JAVA):
    git_init(repo)

# --- 1. mechanical fixes -----------------------------------------------------
print("\n== C# fixes")
check("C# unused using removed", fix(CSHARP, "Demo/Program.cs", "csharpsquid:S1128", "using System.Text;"))
check("C# throw ex -> throw;", fix(CSHARP, "Demo/Program.cs", "csharpsquid:S3445", "throw ex;", "throw ex"))
cs_text = (CSHARP / "Demo/Program.cs").read_bytes().decode()
check("C# file keeps CRLF", "\r\n" in cs_text and "\n" not in cs_text.replace("\r\n", ""))
check("C# fixed text correct", "using System.Text;" not in cs_text and "throw;" in cs_text)

print("\n== JavaScript fixes")
check("JS unused import removed", fix(
    NPM, "index.js", "javascript:S1128", "unused", "unused", "Remove this unused import of 'unused'."))
check("JS import list tidy", 'import { used } from "./lib.js";' in (NPM / "index.js").read_text())

print("\n== Java fixes")
check("Java unused import removed", fix(JAVA, "src/main/java/demo/Greeter.java", "java:S1128", "import java.util.List;"))
check("Java stray ; removed", fix(JAVA, "src/main/java/demo/Greeter.java", "java:S1116", "        ;"))
j_text = (JAVA / "src/main/java/demo/Greeter.java").read_text()
check("Java fixed text correct", "java.util.List" not in j_text and "\n        ;\n" not in j_text)

# --- 2. build verification ---------------------------------------------------
print("\n== build verification")
r = build.verify(str(CSHARP), ["Demo/Program.cs"])
check("C# builds after fixes", r["status"] == "passed", r["output"][-400:])
r = build.verify(str(NPM), ["index.js"])
check("npm/JS builds after fixes", r["status"] == "passed", r["output"][-400:])
check("npm/JS tests run and pass", r["test_status"] == "passed", r["test_output"][-400:])
check("npm artifact reported", bool(r["artifacts"]))
r = build.verify(str(NPM_BREAK), ["index.js"])
check("npm (uuid fixture) builds before upgrade", r["status"] == "passed", r["output"][-400:])
r = build.verify(str(JAVA), ["src/main/java/demo/Greeter.java"])
have_mvn = bool(shutil.which("mvn") or shutil.which("mvn.cmd"))
if have_mvn:
    check("Java builds after fixes", r["status"] == "passed", r["output"][-400:])
else:
    check("Java without Maven reports the missing tool, not a code failure",
          r["status"] == "failed" and "Maven" in r["output"], r["output"][-200:])

# --- 3. dependency scan ------------------------------------------------------
print("\n== dependency scan")
cs = deps.scan_project(str(CSHARP))
nj = find(cs, "Newtonsoft.Json")
check("NuGet: vulnerable Newtonsoft.Json found", bool(nj and nj["severity"] != "none"), str(nj))
check("NuGet: fixed version resolved", bool(nj and nj["fixed_version"]), str(nj))
check("NuGet: latest version recorded", bool(nj and nj["latest_version"]))
rs = find(cs, "RestSharp")
check("NuGet: outdated RestSharp tracked", bool(rs and rs["latest_version"]), str(rs))
js = deps.scan_project(str(NPM))
lo = find(js, "lodash")
check("npm: vulnerable lodash found", bool(lo and lo["severity"] != "none"), str(lo))
check("npm: lodash fix is a safe bump", bool(lo and lo["recommendation"] == "upgrade"), str(lo))
ub = deps.scan_project(str(NPM_BREAK))
uu = find(ub, "uuid")
check("npm: uuid tracked (vulnerable or outdated) with latest", bool(uu and uu["latest_version"]), str(uu))
jv = deps.scan_project(str(JAVA))
jd = find(jv, "com.fasterxml.jackson.core:jackson-databind")
check("Maven: vulnerable jackson-databind found via OSV", bool(jd and jd["severity"] != "none"), str(jd))
check("Maven: latest version recorded", bool(jd and jd["latest_version"]))

# --- 4. upgrade analysis -----------------------------------------------------
print("\n== upgrade analysis (trial upgrade in a scratch copy)")
if lo:
    a = deps_upgrade.analyze_upgrade(str(NPM), lo)
    check("npm lodash upgrade: verdict safe", a["verdict"] == "safe", f"{a['verdict']} {a['reasons']}")
    check("npm lodash upgrade: manifest diff shown", "4.17.15" in a["manifest_diff"], a["manifest_diff"])
    check("npm lodash upgrade: tests ran in trial", a["test_status"] == "passed", str(a["test_status"]))
    check("npm lodash upgrade: usage found", a["usage"]["count"] >= 1, str(a["usage"]))
if uu:
    a = deps_upgrade.analyze_upgrade(str(NPM_BREAK), uu)
    check("npm uuid upgrade: flagged risky (tests break)", a["verdict"] == "risky", f"{a['verdict']} {a['reasons']}")
    check("npm uuid upgrade: error excerpt captured", bool(a["errors"]) or a["test_status"] == "failed")
if nj:
    a = deps_upgrade.analyze_upgrade(str(CSHARP), nj)
    check("NuGet Newtonsoft upgrade: builds (safe or caution)", a["verdict"] in ("safe", "caution"), f"{a['verdict']} {a['reasons']}")
    check("NuGet Newtonsoft upgrade: manifest diff shown", "13." in a["manifest_diff"], a["manifest_diff"])
if rs:
    a = deps_upgrade.analyze_upgrade(str(CSHARP), rs)
    check("NuGet RestSharp upgrade: flagged risky (API removed)", a["verdict"] == "risky", f"{a['verdict']} {a['reasons']} {a['errors']}")
    check("NuGet RestSharp upgrade: compiler errors captured", bool(a["errors"]))
if jd:
    a = deps_upgrade.analyze_upgrade(str(JAVA), jd)
    check("Maven jackson upgrade: manifest diff shown", jd["fixed_version"] in a["manifest_diff"] or "2.9.8" in a["manifest_diff"], a["manifest_diff"])
    if have_mvn:
        check("Maven jackson upgrade: trial build ran", a["build_status"] is not None, str(a["trial"]))
    else:
        skip("Maven trial build", "no Maven on PATH - verdict falls back to semver")
        check("Maven jackson upgrade: semver fallback gives a verdict", a["verdict"] in ("safe", "caution"), str(a["verdict"]))

print("\n== real working trees untouched by analysis")
for name, repo in (("C#", CSHARP), ("npm", NPM), ("npm-break", NPM_BREAK), ("Java", JAVA)):
    # Fixes were applied before the commit-less checks; only manifests must be unchanged.
    out = subprocess.run(["git", "-C", str(repo), "diff", "--name-only"], capture_output=True, text=True).stdout
    manifests = [l for l in out.splitlines() if l.endswith(("csproj", "package.json", "pom.xml"))]
    check(f"{name}: manifest not modified by analysis", not manifests, str(manifests))

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
print(f"Scratch dir: {WORKDIR}")
sys.exit(1 if failed else 0)

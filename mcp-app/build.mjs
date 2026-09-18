import { build } from "esbuild";
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
const result = await build({
  entryPoints: [new URL("app.js", import.meta.url).pathname],
  bundle: true,
  minify: true,
  write: false,
  format: "esm",
  target: "es2022",
  legalComments: "inline",
  metafile: true,
});
const template = (
  await readFile(new URL("index.html", import.meta.url), "utf8")
).replace(
  "<!-- XTERM_CSS -->",
  "<style>" +
    (await readFile(
      new URL("node_modules/@xterm/xterm/css/xterm.css", import.meta.url),
      "utf8",
    )).replaceAll("\t", "  ") +
    "</style>",
);
const script = result.outputFiles[0].text.replaceAll("</script", "<\\/script");
await writeFile(
  new URL("../src/tmux_fleet/resources/mcp_app.html", import.meta.url),
  template.replace(
    "<!-- APP_SCRIPT -->",
    () => `<script type="module">${script}</script>`,
  ),
);
const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const pieces = path
    .resolve(input)
    .split(`${path.sep}node_modules${path.sep}`);
  if (pieces.length < 2) continue;
  const tail = pieces.at(-1).split(path.sep);
  packages.add(
    pieces.slice(0, -1).join(`${path.sep}node_modules${path.sep}`) +
      `${path.sep}node_modules${path.sep}` +
      tail.slice(0, tail[0].startsWith("@") ? 2 : 1).join(path.sep),
  );
}
let licenses =
  "Third-party notices for the bundled tmux-fleet MCP Apps view\n\n";
for (const directory of [...packages].sort()) {
  const metadata = JSON.parse(
    await readFile(path.join(directory, "package.json"), "utf8"),
  );
  const license = (await readdir(directory)).find((name) =>
    /^licen[cs]e(?:\..*)?$/i.test(name),
  );
  if (!license) throw new Error(`Missing license for ${metadata.name}`);
  licenses += `${metadata.name} ${metadata.version}\n${await readFile(path.join(directory, license), "utf8")}\n\n`;
}
await writeFile(
  new URL("../src/tmux_fleet/resources/mcp_app.LICENSE.txt", import.meta.url),
  licenses.replace(/[ \t]+$/gm, "").trimEnd() + "\n",
);

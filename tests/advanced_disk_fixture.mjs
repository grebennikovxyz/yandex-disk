// In-process Disk fixture for the optional engine integration test. No network access.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { posix } from "node:path";
import { pathToFileURL } from "node:url";

const filename = process.env.FIXTURE_DISK;
const hash = (method, text) => createHash(method).update(text).digest("hex");
const revision = (text) => parseInt(hash("sha256", text).slice(0, 12), 16);
const bytesOf = (value) => typeof value === "string" ? Buffer.from(value) : Buffer.from(value.base64, "base64");
const clean = (path) => path.replace(/^disk:/, "");
const reply = (value, status = 200) => Response.json(value, { status });

function metadata(path, text) {
  const folder = text === null;
  const bytes = folder ? Buffer.from(path) : bytesOf(text);
  return {
    path: "disk:" + path, name: posix.basename(path), type: folder ? "dir" : "file",
    resource_id: hash("sha256", path), revision: revision(bytes),
    modified: "2026-01-02T00:00:00Z", mime_type: folder ? "inode/directory" : "text/plain",
    ...(folder ? {} : { size: bytes.length, md5: hash("md5", bytes), sha256: hash("sha256", bytes) })
  };
}

globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  const url = new URL(request.url);
  const files = JSON.parse(readFileSync(filename, "utf8"));
  const path = clean(url.searchParams.get("path") || "/");
  const text = files[path];
  const save = () => writeFileSync(filename, JSON.stringify(files));
  if (url.hostname === "downloader.disk.yandex.ru") {
    if (request.headers.has("authorization")) throw Error("credential on transfer");
    if (request.method === "PUT") {
      const data = Buffer.from(await request.arrayBuffer());
      files[path] = data.subarray(0, 2).toString() === "PK" ? { base64: data.toString("base64") } : data.toString("utf8");
      save();
      return new Response(null, {status: 201});
    }
    return new Response(bytesOf(text));
  }
  if (url.hostname !== "cloud-api.yandex.net") throw Error("unexpected host");
  if (!request.headers.get("authorization")?.includes("synthetic-account")) return reply({}, 401);
  switch (url.pathname) {
    case "/v1/disk":
      return reply({user: {uid: "123", login: "sample"}, revision: revision(JSON.stringify(files))});
    case "/v1/disk/resources":
      if (text === undefined) return reply({}, 404);
      if (request.method === "DELETE") {
        if (url.searchParams.get("md5") !== hash("md5", bytesOf(text))) return reply({}, 409);
        delete files[path]; save();
        return new Response(null, {status: 204});
      }
      return reply({...metadata(path, text), _embedded: {
        items: Object.entries(files).filter(([p]) => p !== path && posix.dirname(p) === path)
            .map(([p, t]) => metadata(p, t)),
        offset: 0, total: Object.keys(files).filter(p => p !== path && posix.dirname(p) === path).length
      }});
    case "/v1/disk/resources/files":
      return reply({items: Object.entries(files).filter(([,t]) => t !== null).map(([p,t]) => metadata(p,t))});
    case "/v1/disk/resources/download":
    case "/v1/disk/resources/upload":
      return reply({href: "https://downloader.disk.yandex.ru/file?path=" + encodeURIComponent(path)});
    case "/v1/disk/resources/move": {
      const from = clean(url.searchParams.get("from"));
      if (files[from] === undefined || text !== undefined) return reply({}, 409);
      files[path] = files[from]; delete files[from]; save();
      return reply({href: "https://cloud-api.yandex.net/v1/disk/resources", method: "GET"}, 201);
    }
    default: return reply({}, 404);
  }
};

await import(pathToFileURL(process.env.FIXTURE_PROGRAM));

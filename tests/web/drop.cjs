// Drops folders of this machine on a page's folder box the way a browser hands dragged folders over
// (FileSystemDirectoryEntry), for the browser tests. dropOn(page, folders, { asFiles, slow }): folders is a list
// of folder paths; asFiles drops the first file of each instead; slow delays each read of a folder (ms).
const fs = require('fs');
const path = require('path');

const walk = (dir, rel) => fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1))
  .flatMap((d) => (d.isDirectory() ? walk(path.join(dir, d.name), `${rel}/${d.name}`)
    : [[`${rel}/${d.name}`, fs.readFileSync(path.join(dir, d.name)).toString('base64')]]));

async function dropOn(page, folders, { asFiles = false, slow = 0 } = {}) {
  const given = folders.map((f) => { const top = `/${path.basename(f)}`; return [top, walk(f, top)]; });
  await page.$eval('.drop', (box, [given, asFiles, slow]) => {
    const dirs = new Map();
    const dir = (fullPath) => {
      if (!dirs.has(fullPath)) {
        const kids = [];
        dirs.set(fullPath, { isDirectory: true, isFile: false, name: fullPath.slice(fullPath.lastIndexOf('/') + 1), fullPath, kids,
          createReader() { let done = false; return { readEntries(ok) { setTimeout(() => { ok(done ? [] : kids); done = true; }, slow); } }; } });
        const up = fullPath.slice(0, fullPath.lastIndexOf('/'));
        if (up) dir(up).kids.push(dirs.get(fullPath));
      }
      return dirs.get(fullPath);
    };
    const entries = given.map(([top, files]) => {
      const leaves = files.map(([fp, b64]) => {
        const name = fp.slice(fp.lastIndexOf('/') + 1);
        const file = new File([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))], name);
        const entry = { isDirectory: false, isFile: true, name, fullPath: fp, file: (ok) => ok(file) };
        dir(fp.slice(0, fp.lastIndexOf('/'))).kids.push(entry);
        return entry;
      });
      return asFiles ? leaves[0] : dir(top);
    });
    const ev = new Event('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'dataTransfer', { value: { types: ['Files'], items: entries.map((e) => ({ kind: 'file', webkitGetAsEntry: () => e })) } });
    box.dispatchEvent(ev);
  }, [given, asFiles, slow]);
}

module.exports = { dropOn };

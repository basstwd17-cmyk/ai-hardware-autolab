// A minimal stand-in for the office UI bridge, run as a separate process.
//
// It exists because every in-process Python test passed while the real Node
// bridge killed the accept loop on its first connection. A client in the same
// process shares the GIL and the timing; a real one does not, and that is the
// difference the pipe bug lived in.
//
//   node ui_bridge_client.mjs <pipe> <after> <framesWanted> [holdMs] [commandJson]
//
// Prints one JSON object on stdout describing what it saw, then exits.

import net from "node:net";

const [pipe, afterRaw, wantedRaw, holdRaw, commandRaw] = process.argv.slice(2);
const after = Number(afterRaw ?? 0);
const wanted = Number(wantedRaw ?? 1);
const holdMs = Number(holdRaw ?? 0);

const seen = [];
const errors = [];
let buffered = "";
let ready = null;
let settled = false;

const socket = net.connect(pipe);

function finish(reason) {
  if (settled) return;
  settled = true;
  try {
    socket.destroy();
  } catch {
    /* already gone */
  }
  process.stdout.write(
    JSON.stringify({
      reason,
      ready,
      frames: seen,
      errors,
      connected: ready !== null,
    }) + "\n",
  );
  process.exit(0);
}

socket.on("connect", () => {
  socket.write(JSON.stringify({ type: "subscribe", protocol: 1, after }) + "\n");
  if (commandRaw) socket.write(commandRaw + "\n");
});

socket.on("data", (chunk) => {
  buffered += chunk.toString("utf8");
  let index;
  while ((index = buffered.indexOf("\n")) >= 0) {
    const line = buffered.slice(0, index).trim();
    buffered = buffered.slice(index + 1);
    if (!line) continue;
    let frame;
    try {
      frame = JSON.parse(line);
    } catch (error) {
      errors.push("bad JSON: " + line.slice(0, 80));
      continue;
    }
    seen.push({
      type: frame.type,
      cursor: frame.cursor ?? frame.event?.event_id ?? null,
      kind: frame.event?.kind ?? null,
      ok: frame.ok ?? null,
      commandId: frame.commandId ?? null,
      jobId: frame.job?.job_id ?? frame.event?.job_id ?? null,
      preflightStatus: frame.preflight?.status ?? null,
      selectedContract: frame.preflight?.selected_contract?.name ?? null,
      executionMode: frame.preflight?.execution_mode ?? null,
    });
    if (frame.type === "ready") ready = frame.cursor;
    if (seen.length >= wanted && ready !== null) {
      if (holdMs > 0) setTimeout(() => finish("held"), holdMs);
      else finish("wanted");
    }
  }
});

socket.on("error", (error) => {
  errors.push(error.code || String(error));
  if (!settled) finish("error");
});

socket.on("close", () => finish("closed"));

setTimeout(() => finish("timeout"), 20000);

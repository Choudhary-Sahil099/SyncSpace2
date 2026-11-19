import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  Cloud,
  FilePlus2,
  FileText,
  Lock,
  LogOut,
  MoreHorizontal,
  Plus,
  Share2,
  Sparkles,
  Users,
  Wifi,
  WifiOff,
} from "lucide-react";
import { api, Doc, DocSummary, getToken, setToken, User, WS_BASE } from "./api";
import { makeEdit, rebaseDraft } from "./ot-client";
import "./style.css";
import {
  RemoteCursorOverlay,
  cursorColor,
  type RemoteCursor,
} from "./remote-cursors";

type AuthResult = { token: string; user: User };
type WsEvent = {
  type: string;
  content?: string;
  revision?: number;
  current_revision?: number;
  operation_id?: string;
  message?: string;
  role?: Doc["role"];
  users?: string[];
  username?: string;
  connection_id?: string;
  position?: number;
  anchor?: number;
  cursors?: WsEvent[];
};

function generateOperationId(): string {
  const bytes = new Uint8Array(16);

  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    throw new Error("Secure random number generation is unavailable");
  }

  // UUID version 4
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));

  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

function transformCaret(
  oldText: string,
  newText: string,
  position: number,
): number {
  let start = 0;

  while (
    start < oldText.length &&
    start < newText.length &&
    oldText[start] === newText[start]
  ) {
    start++;
  }

  let oldEnd = oldText.length;
  let newEnd = newText.length;

  while (
    oldEnd > start &&
    newEnd > start &&
    oldText[oldEnd - 1] === newText[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }

  // Position occurs before the changed region.
  if (position < start) return position;

  // Position occurs after the changed region.
  if (position > oldEnd) {
    return position + (newEnd - oldEnd);
  }

  // Position is inside or at the change.
  return newEnd;
}

function App() {
  const [user, setUser] = useState<User | null>(null),
    [authMode, setAuthMode] = useState<"login" | "register">("register");
  const [docs, setDocs] = useState<DocSummary[]>([]),
    [active, setActive] = useState<Doc | null>(null);
  const [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [loginUser, setLoginUser] = useState(""),
    [loginPass, setLoginPass] = useState("");
  const [draft, setDraft] = useState(""),
    [rev, setRev] = useState(0),
    [connected, setConnected] = useState(false),
    [saved, setSaved] = useState(true),
    [presence, setPresence] = useState<string[]>([]);
  const [shareOpen, setShareOpen] = useState(false),
    [invite, setInvite] = useState(""),
    [inviteRole, setInviteRole] = useState("editor"),
    [shareMessage, setShareMessage] = useState("");
  const [snapshotMessage, setSnapshotMessage] = useState("");
  const [connectionStatus, setConnectionStatus] = useState<
    "connecting" | "connected" | "reconnecting" | "offline"
  >("connecting");
  const ws = useRef<WebSocket | null>(null),
    shadow = useRef(""),
    revision = useRef(0),
    draftRef = useRef(""),
    outstanding = useRef<{
      id: string;
      base: string;
      submitted: string;
      payload: Record<string, unknown>;
    } | null>(null),
    pendingTimeout = useRef<ReturnType<typeof setTimeout> | null>(null),
    docId = useRef("");
  const socketReady = useRef(false);
  const recoveryBlocked = useRef(false);
  const [docSearch, setDocSearch] = useState("");
  const [remoteCursors, setRemoteCursors] = useState<
    Record<string, RemoteCursor>
  >({});
  const [editorScroll, setEditorScroll] = useState({ top: 0, left: 0 });
  const ownConnection = useRef("");
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const localSelection = useRef({
    start: 0,
    end: 0,
    direction: "none" as "forward" | "backward" | "none",
  });

  const rememberSelection = () => {
    const editor = editorRef.current;
    if (!editor) return;

    localSelection.current = {
      start: editor.selectionStart,
      end: editor.selectionEnd,
      direction: editor.selectionDirection,
    };
  };

  const notifyCursor = () => {
    const el = editorRef.current;
    if (!el || ws.current?.readyState !== WebSocket.OPEN) return;
    const position = Array.from(el.value.slice(0, el.selectionEnd)).length;
    const anchor = Array.from(el.value.slice(0, el.selectionStart)).length;
    ws.current.send(
      JSON.stringify({
        type: "cursor",
        position,
        anchor,
        revision: revision.current,
      }),
    );
  };

  const reload = async () => {
    setDocs(await api<DocSummary[]>("/documents"));
  };
  useEffect(() => {
    (async () => {
      if (getToken()) {
        try {
          setUser(await api<User>("/me"));
          await reload();
        } catch {
          setToken(null);
        }
      }
      setLoading(false);
    })();
  }, []);
  const logout = () => {
    ws.current?.close();
    setToken(null);
    setUser(null);
    setActive(null);
    setDocs([]);
  };
  const submitAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const result = await api<AuthResult>(`/auth/${authMode}`, "POST", {
        username: loginUser,
        password: loginPass,
      });
      setToken(result.token);
      setUser(result.user);
      await reload();
    } catch (e) {
      setError(String((e as Error).message));
    }
  };
  const create = async () => {
    try {
      const d = await api<Doc>("/documents", "POST", {
        title: "Untitled document",
      });
      await reload();
      setActive(d);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const selectDoc = async (id: string) => {
    try {
      setActive(await api<Doc>(`/documents/${id}`));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const flush = () => {
    if (
      recoveryBlocked.current ||
      !socketReady.current ||
      outstanding.current ||
      !ws.current ||
      ws.current.readyState !== WebSocket.OPEN
    ) {
      return;
    }

    const edit = makeEdit(shadow.current, draftRef.current);

    if (!edit) {
      setSaved(true);
      return;
    }

    const id = generateOperationId();

    const payload = {
      type: "edit",
      operation_id: id,
      base_revision: revision.current,
      ...edit,
    };

    outstanding.current = {
      id,
      base: shadow.current,
      submitted: draftRef.current,
      payload,
    };

    setSaved(false);

    try {
      ws.current.send(JSON.stringify(payload));
    } catch {
      ws.current?.close();
    }
  };

  const updateDraftPreservingSelection = (nextText: string) => {
    const editor = editorRef.current;
    const oldText = editor?.value ?? draftRef.current;

    const oldSelection = { ...localSelection.current };

    const start = transformCaret(oldText, nextText, oldSelection.start);

    const end = transformCaret(oldText, nextText, oldSelection.end);

    localSelection.current = {
      ...oldSelection,
      start,
      end,
    };

    draftRef.current = nextText;
    setDraft(nextText);

    requestAnimationFrame(() => {
      const el = editorRef.current;
      if (!el) return;

      el.setSelectionRange(
        Math.min(start, el.value.length),
        Math.min(end, el.value.length),
        oldSelection.direction,
      );
    });
  };

  const scheduleFlush = (delay = 160) => {
    if (pendingTimeout.current) clearTimeout(pendingTimeout.current);
    pendingTimeout.current = setTimeout(flush, delay);
  };

  const acceptServer = (message: WsEvent) => {
    const next = message.content ?? "";
    const nextRev = message.current_revision ?? message.revision ?? 0;

    // Handle ACK for our own outstanding operation first.
    if (
      message.type === "ack" &&
      outstanding.current &&
      message.operation_id === outstanding.current.id
    ) {
      const operation = outstanding.current;

      try {
        const recovered = rebaseDraft(
          operation.submitted,
          draftRef.current,
          next,
        );

        outstanding.current = null;

        shadow.current = next;
        revision.current = nextRev;
        updateDraftPreservingSelection(recovered);
        setRev(nextRev);
        setSaved(recovered === next);

        scheduleFlush(15);
      } catch {
        recoveryBlocked.current = true;
        outstanding.current = null;
        setSaved(false);
        setError(
          "Automatic reconciliation failed. Your local text is preserved. Copy it before reloading.",
        );
      }

      return;
    }

    // Ignore unexpected acknowledgements.
    if (message.type === "ack") return;

    // Never apply an old server revision.
    if (nextRev <= revision.current) return;

    // Wait for our outstanding ACK before merging broadcasts.
    if (outstanding.current) return;

    try {
      const recovered = rebaseDraft(shadow.current, draftRef.current, next);

      shadow.current = next;
      revision.current = nextRev;
      updateDraftPreservingSelection(recovered);
      setRev(nextRev);
      setSaved(recovered === next);

      scheduleFlush();
    } catch {
      recoveryBlocked.current = true;
      setSaved(false);
      setError(
        "Concurrent changes could not be merged automatically. Your local text remains available.",
      );
    }
  };

  useEffect(() => {
    const token = getToken();
    if (!active || !token) return;

    const currentDocId = active.id;

    docId.current = currentDocId;
    shadow.current = active.content;
    revision.current = active.revision;
    draftRef.current = active.content;
    outstanding.current = null;
    socketReady.current = false;
    recoveryBlocked.current = false;

    setDraft(active.content);
    setRev(active.revision);
    setSaved(true);
    setConnected(false);
    setConnectionStatus("connecting");
    setPresence([]);
    setRemoteCursors({});
    ownConnection.current = "";
    setEditorScroll({ top: 0, left: 0 });

    let disposed = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let retryCount = 0;

    const connect = () => {
      if (disposed || recoveryBlocked.current) return;

      setConnectionStatus(retryCount === 0 ? "connecting" : "reconnecting");

      const socket = new WebSocket(
        `${WS_BASE}/ws/${currentDocId}?token=${encodeURIComponent(token)}`,
      );

      ws.current = socket;
      socketReady.current = false;

      socket.onopen = () => {
        if (disposed || ws.current !== socket) return;

        // Do not send edits yet.
        // Wait for the server's init message.
      };

      socket.onmessage = (event: MessageEvent) => {
        if (disposed || ws.current !== socket) return;

        let msg: WsEvent;

        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }

        // INITIALIZATION AND RECOVERY
        if (msg.type === "init") {
          ownConnection.current = msg.connection_id || "";

          const serverText = msg.content ?? "";
          const serverRev = msg.revision ?? 0;
          const inFlight = outstanding.current;

          if (inFlight) {
            // The edit may already have committed.
            // Retry the original payload with the same operation ID.
            try {
              socket.send(JSON.stringify(inFlight.payload));
            } catch {
              socket.close();
              return;
            }
          } else {
            // Merge unsent local changes onto the server state.
            try {
              const recovered = rebaseDraft(
                shadow.current,
                draftRef.current,
                serverText,
              );

              shadow.current = serverText;
              revision.current = serverRev;

              updateDraftPreservingSelection(recovered);

              setRev(serverRev);
              setSaved(recovered === serverText);
            } catch {
              recoveryBlocked.current = true;
              setSaved(false);
              setError(
                "Recovery failed. Your local text has been preserved. Copy it before reloading.",
              );
              socket.close();
              return;
            }
          }

          socketReady.current = true;
          retryCount = 0;

          setConnected(true);
          setConnectionStatus("connected");

          socket.send(JSON.stringify({ type: "presence" }));

          if (!inFlight) {
            scheduleFlush(100);
          }

          requestAnimationFrame(() => {
            if (!disposed && ws.current === socket) {
              notifyCursor();
            }
          });

          return;
        }

        // EDIT ACKNOWLEDGEMENTS AND REMOTE OPERATIONS
        if (msg.type === "ack" || msg.type === "operation") {
          acceptServer(msg);
          return;
        }

        // ONLINE COLLABORATORS
        if (msg.type === "presence") {
          setPresence(msg.users || []);
          return;
        }

        // EXISTING REMOTE CURSORS
        if (msg.type === "cursors") {
          const found: Record<string, RemoteCursor> = {};

          for (const c of msg.cursors || []) {
            if (
              c.connection_id &&
              c.connection_id !== ownConnection.current &&
              c.username
            ) {
              found[c.connection_id] = {
                connection_id: c.connection_id,
                username: c.username,
                position: c.position ?? 0,
                anchor: c.anchor ?? c.position ?? 0,
                revision: c.revision ?? 0,
                seen: Date.now(),
              };
            }
          }

          setRemoteCursors(found);
          return;
        }

        // LIVE CURSOR AND SELECTION UPDATES
        if (
          msg.type === "cursor" &&
          msg.connection_id &&
          msg.connection_id !== ownConnection.current &&
          msg.username
        ) {
          const c = msg;

          setRemoteCursors((prev) => ({
            ...prev,
            [c.connection_id!]: {
              connection_id: c.connection_id!,
              username: c.username!,
              position: c.position ?? 0,
              anchor: c.anchor ?? c.position ?? 0,
              revision: c.revision ?? 0,
              seen: Date.now(),
            },
          }));

          return;
        }

        // COLLABORATOR DISCONNECTED
        if (msg.type === "cursor_leave" && msg.connection_id) {
          setRemoteCursors((prev) => {
            const copy = { ...prev };
            delete copy[msg.connection_id!];
            return copy;
          });

          return;
        }

        // SERVER REJECTED AN OPERATION
        if (msg.type === "error") {
          setError(msg.message || "Edit rejected");

          // An explicit rejection means this operation cannot
          // continue unchanged. Fetch the authoritative state.
          if (
            outstanding.current &&
            msg.operation_id &&
            msg.operation_id !== outstanding.current.id
          ) {
            return;
          }

          outstanding.current = null;
          recoveryBlocked.current = true;
          setSaved(false);

          const localBase = shadow.current;

          api<Doc>(`/documents/${currentDocId}`)
            .then((d) => {
              if (disposed || ws.current !== socket) return;

              try {
                const recovered = rebaseDraft(
                  localBase,
                  draftRef.current,
                  d.content,
                );

                shadow.current = d.content;
                revision.current = d.revision;
                draftRef.current = recovered;

                setDraft(recovered);
                setRev(d.revision);
                setSaved(recovered === d.content);

                recoveryBlocked.current = false;
                scheduleFlush(100);
              } catch {
                setError(
                  "Automatic merge failed. Your local edits are preserved. Copy them before reloading.",
                );
              }
            })
            .catch(() => {
              setError(
                "Unable to resynchronize. Your local edits are preserved.",
              );
            });

          return;
        }
      };

      socket.onerror = () => {
        if (!disposed && ws.current === socket) {
          socket.close();
        }
      };

      socket.onclose = () => {
        if (disposed || ws.current !== socket) return;

        socketReady.current = false;

        setConnected(false);
        setPresence([]);
        setRemoteCursors({});
        setConnectionStatus("offline");

        // Never discard unacknowledged edits here.
        // They may already be committed on the server.

        if (recoveryBlocked.current) return;

        retryCount++;

        const exponentialDelay = Math.min(
          1000 * 2 ** Math.min(retryCount - 1, 5),
          30000,
        );

        const jitter = Math.random() * 300;
        const delay = exponentialDelay + jitter;

        reconnectTimer = setTimeout(connect, delay);
      };
    };

    // Shared intervals: create them only once per document.
    const cursorCleanup = setInterval(() => {
      setRemoteCursors((prev) =>
        Object.fromEntries(
          Object.entries(prev).filter(([, c]) => Date.now() - c.seen < 40000),
        ),
      );
    }, 5000);

    const heartbeat = setInterval(() => {
      const socket = ws.current;

      if (socketReady.current && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping" }));
        socket.send(JSON.stringify({ type: "presence" }));
      }
    }, 12000);

    connect();

    // Cleanup when leaving or changing documents.
    return () => {
      disposed = true;
      socketReady.current = false;

      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }

      clearInterval(cursorCleanup);
      clearInterval(heartbeat);

      if (pendingTimeout.current) {
        clearTimeout(pendingTimeout.current);
        pendingTimeout.current = null;
      }

      ws.current?.close();
      ws.current = null;
    };
  }, [active?.id]);

  const editDraft = (text: string) => {
    draftRef.current = text;
    setDraft(text);
    setSaved(false);
    scheduleFlush();
    requestAnimationFrame(notifyCursor);
  };
  const rename = async (title: string) => {
    if (!active || !title.trim()) return;
    try {
      const d = await api<Doc>(`/documents/${active.id}`, "PATCH", { title });
      setActive((prev) => (prev ? { ...prev, title: d.title } : null));
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const share = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!active) return;
    try {
      const result = await api<{ username: string; role: string }>(
        `/documents/${active.id}/share`,
        "POST",
        { username: invite, role: inviteRole },
      );
      setShareMessage(
        `${result.username} can now ${result.role === "editor" ? "edit" : "view"} this document.`,
      );
      setInvite("");
    } catch (e) {
      setShareMessage((e as Error).message);
    }
  };
  const saveSnapshot = async () => {
    if (!active) return;
    try {
      await api(`/documents/${active.id}/snapshot`, "POST");
      setSnapshotMessage(
        "Snapshot requested. The worker will save it to MinIO.",
      );
    } catch (e) {
      setSnapshotMessage((e as Error).message);
    }
  };
  const filtered = useMemo(
    () =>
      docs.filter((d) =>
        d.title.toLowerCase().includes(docSearch.toLowerCase()),
      ),
    [docs, docSearch],
  );
  if (loading) return <div className="loading">Loading SyncSpace…</div>;
  if (!user)
    return (
      <div className="auth-layout">
        <div className="brand-panel">
          <div className="brand">
            <div className="brand-icon">
              <Sparkles size={24} />
            </div>
            SyncSpace
          </div>
          <div className="hero">
            <div className="eyebrow">THE COLLABORATIVE WORKSPACE</div>
            <h1>
              Great ideas
              <br />
              happen <span>together.</span>
            </h1>
            <p>
              Write, create, and collaborate on documents in real time. One
              workspace. Endless possibilities.
            </p>
            <div className="feature-line">
              <span className="green-dot" /> Real-time editing <span>✦</span>{" "}
              Instant sync <span>✦</span> Secure sharing
            </div>
          </div>
          <div className="glow glow-one" />
          <div className="glow glow-two" />
        </div>
        <div className="auth-panel">
          <form className="auth-card" onSubmit={submitAuth}>
            <div className="logo-mobile">✦ SyncSpace</div>
            <div className="eyebrow">WELCOME TO SYNCSPACE</div>
            <h2>
              {authMode === "register" ? "Create your account" : "Welcome back"}
            </h2>
            <p>
              {authMode === "register"
                ? "Start making something amazing with your team."
                : "Your documents are right where you left them."}
            </p>
            <label>
              Username
              <input
                autoComplete="username"
                placeholder="your_username"
                value={loginUser}
                onChange={(e) => setLoginUser(e.target.value)}
                minLength={3}
                required
              />
            </label>
            <label>
              Password
              <input
                type="password"
                autoComplete={
                  authMode === "login" ? "current-password" : "new-password"
                }
                placeholder="At least 8 characters"
                minLength={8}
                value={loginPass}
                onChange={(e) => setLoginPass(e.target.value)}
                required
              />
            </label>
            {error && <div className="error">{error}</div>}
            <button className="primary full" type="submit">
              {authMode === "register" ? "Create account" : "Sign in"}{" "}
              <ChevronRight size={17} />
            </button>
            <div className="switch">
              {authMode === "register"
                ? "Already have an account?"
                : "New to SyncSpace?"}{" "}
              <button
                type="button"
                onClick={() => {
                  setAuthMode(authMode === "login" ? "register" : "login");
                  setError("");
                }}
              >
                {authMode === "register" ? "Sign in" : "Create account"}
              </button>
            </div>
          </form>
        </div>
      </div>
    );
  if (!active)
    return (
      <div className="app-shell">
        <aside className="sidebar">
          <div className="brand sidebar-brand">
            <div className="brand-icon">
              <Sparkles size={18} />
            </div>
            SyncSpace
          </div>
          <div className="workspace-heading">WORKSPACE</div>
          <div className="nav-item selected">
            <FileText size={18} /> All documents <span>{docs.length}</span>
          </div>
          <button className="sidebar-new" onClick={create}>
            <Plus size={18} /> New document
          </button>
          <div className="sidebar-bottom">
            <div className="user-avatar">{user.username[0].toUpperCase()}</div>
            <div className="username">
              <strong>{user.username}</strong>
              <small>Personal workspace</small>
            </div>
            <button
              className="icon-btn"
              aria-label="Log out"
              title="Log out"
              onClick={logout}
            >
              <LogOut size={18} />
            </button>
          </div>
        </aside>
        <main className="dashboard">
          <div className="topbar">
            <span>My workspace</span>
            <div className="topbar-right">
              <span className="avatar-small">
                {user.username[0].toUpperCase()}
              </span>
            </div>
          </div>
          <div className="dashboard-body">
            <div className="welcome-eyebrow">YOUR CREATIVE SPACE</div>
            <h1>
              Your documents<span className="title-dot">.</span>
            </h1>
            <p className="subtle">
              Every great idea starts somewhere. Pick up where you left off.
            </p>
            <div className="dash-actions">
              <button className="primary" onClick={create}>
                <Plus size={19} /> New document
              </button>
              <input
                className="search-input"
                placeholder="Search documents..."
                value={docSearch}
                onChange={(e) => setDocSearch(e.target.value)}
              />
            </div>
            <div className="section-heading">
              Recent documents <span>{filtered.length}</span>
            </div>
            {error && <div className="error">{error}</div>}
            <div className="doc-grid">
              <button className="new-card" onClick={create}>
                <span className="new-icon">
                  <FilePlus2 size={30} />
                </span>
                <strong>Create new document</strong>
                <small>Start with a blank page</small>
              </button>
              {filtered.map((d) => (
                <button
                  className="doc-card"
                  onClick={() => selectDoc(d.id)}
                  key={d.id}
                >
                  <div className="doc-icon">
                    <FileText size={25} />
                  </div>
                  <h3>{d.title}</h3>
                  <p>
                    {d.owner ? "Owned by you" : "Shared with you"} · v
                    {d.revision}
                  </p>
                  <small>
                    Updated {new Date(d.updated_at).toLocaleDateString()}
                  </small>
                </button>
              ))}
            </div>
          </div>
        </main>
      </div>
    );
  return (
    <div className="editor-page">
      <header className="editor-header">
        <button
          className="icon-btn"
          title="Back to documents"
          onClick={() => {
            ws.current?.close();
            setActive(null);
            reload();
          }}
        >
          <ArrowLeft size={21} />
        </button>
        <div className="brand-icon compact">
          <Sparkles size={17} />
        </div>
        <div className="editor-title">
          <input
            aria-label="Document title"
            value={active.title}
            disabled={active.role === "viewer"}
            onChange={(e) => setActive({ ...active, title: e.target.value })}
            onBlur={(e) => rename(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
          <div className="status">
            {connected ? <Wifi size={13} /> : <WifiOff size={13} />}{" "}
            {connectionStatus === "connected"
              ? saved
                ? "All changes saved"
                : "Saving changes…"
              : connectionStatus === "reconnecting"
                ? "Reconnecting…"
                : connectionStatus === "connecting"
                  ? "Connecting…"
                  : "Offline — changes pending"}{" "}
            <span>·</span> Revision {rev}
          </div>
        </div>
        <div className="header-actions">
          <span className="collab-bubble">
            <Users size={15} /> {presence.length || 1} online
          </span>
          <button
            className="outline-btn"
            onClick={() => {
              setShareOpen(true);
              setShareMessage("");
            }}
          >
            <Share2 size={16} /> Share
          </button>
          <span className="avatar-small">{user.username[0].toUpperCase()}</span>
        </div>
      </header>
      <div className="editor-toolbar">
        <span className="toolbar-label">DOCUMENT</span>
        <span className="toolbar-separator" />
        <span className="toolbar-info">
          <Cloud size={15} /> Auto-saved with version history
        </span>
        <div className="toolbar-right">
          <button onClick={saveSnapshot} className="toolbar-action">
            Save snapshot
          </button>
          <span className="permission">{active.role}</span>
        </div>
      </div>
      <div className="document-stage">
        <div className="remote-presence" aria-label="Collaborator cursors">
          {Object.values(remoteCursors).map((c) => (
            <span
              className="remote-person"
              key={c.connection_id}
              title={
                c.revision === rev
                  ? `Cursor at position ${c.position}`
                  : "Cursor updating after edit"
              }
            >
              <span
                className="remote-dot"
                style={{ backgroundColor: cursorColor(c.connection_id) }}
              />
              {c.username}
              {c.revision === rev ? ` · ${c.position}` : " · updating"}
            </span>
          ))}
        </div>
        <div className="paper">
          <div className="editor-stack">
            <textarea
              ref={editorRef}
              spellCheck
              className="document-editor"
              aria-label="Document content"
              placeholder="Start writing your story…"
              value={draft}
              disabled={active.role === "viewer"}
              onChange={(e) => {
                rememberSelection();
                editDraft(e.target.value);
              }}
              onSelect={() => {
                rememberSelection();
                notifyCursor();
              }}
              onClick={() => {
                rememberSelection();
                notifyCursor();
              }}
              onKeyUp={() => {
                rememberSelection();
                notifyCursor();
              }}
              onScroll={(e) =>
                setEditorScroll({
                  top: e.currentTarget.scrollTop,
                  left: e.currentTarget.scrollLeft,
                })
              }
            />
            <RemoteCursorOverlay
              text={draft}
              cursors={Object.values(remoteCursors).filter(
                (c) => c.revision === rev,
              )}
              scrollTop={editorScroll.top}
              scrollLeft={editorScroll.left}
            />
          </div>
        </div>
        <div className="document-footer">
          <span>
            {draft.trim() ? draft.trim().split(/\s+/).length : 0} words
          </span>
          <span>{Array.from(draft).length} characters</span>
          <span>Plain text · OT collaboration</span>
        </div>
        {snapshotMessage && (
          <div className="toast" onClick={() => setSnapshotMessage("")}>
            {snapshotMessage}
          </div>
        )}
        {error && (
          <div className="toast alert" onClick={() => setError("")}>
            {error} <span>×</span>
          </div>
        )}
      </div>
      {shareOpen && (
        <div className="modal-backdrop" onClick={() => setShareOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-top">
              <div>
                <h2>Share document</h2>
                <p>Invite people to collaborate on “{active.title}”.</p>
              </div>
              <button className="icon-btn" onClick={() => setShareOpen(false)}>
                ×
              </button>
            </div>
            {active.role === "owner" ? (
              <form onSubmit={share}>
                <label>
                  Invite by username
                  <input
                    value={invite}
                    onChange={(e) => setInvite(e.target.value)}
                    placeholder="teammate_username"
                    required
                  />
                </label>
                <label>
                  Access level
                  <select
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value)}
                  >
                    <option value="editor">Editor — can make changes</option>
                    <option value="viewer">Viewer — read only</option>
                  </select>
                </label>
                <button className="primary full" type="submit">
                  <Share2 size={16} /> Share access
                </button>
                {shareMessage && (
                  <p className="share-message">{shareMessage}</p>
                )}
              </form>
            ) : (
              <p>Only the document owner can invite collaborators.</p>
            )}
            <div className="modal-note">
              <Lock size={15} /> Only invited accounts can access this document.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

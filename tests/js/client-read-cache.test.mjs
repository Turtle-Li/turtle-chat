import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

class MemoryStorage {
  #values = new Map();

  get length() {
    return this.#values.size;
  }

  key(index) {
    return [...this.#values.keys()][index] ?? null;
  }

  getItem(key) {
    return this.#values.has(String(key)) ? this.#values.get(String(key)) : null;
  }

  setItem(key, value) {
    this.#values.set(String(key), String(value));
  }

  removeItem(key) {
    this.#values.delete(String(key));
  }

  values() {
    return [...this.#values.values()];
  }
}

class FakeElement {}

class FakeCustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
  }
}

const json = (payload, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const createHarness = async (options = {}) => {
  const localStorage = options.localStorage ?? new MemoryStorage();
  const sessionStorage = options.sessionStorage ?? new MemoryStorage();
  const documentListeners = new Map();
  const windowListeners = new Map();
  const networkCalls = options.networkCalls ?? [];

  if (!localStorage.getItem("token")) localStorage.setItem("token", "token-a");

  const nativeFetch = async (input, init = {}) => {
    const request = input instanceof Request ? input : null;
    const url = new URL(request?.url || input, "https://chat.example.test/");
    const method = String(init.method || request?.method || "GET").toUpperCase();
    const headers = new Headers(request?.headers || {});
    if (init.headers) {
      new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    }
    networkCalls.push({
      path: `${url.pathname}${url.search}`,
      method,
      authorization: headers.get("Authorization") || "",
    });
    await new Promise((resolve) => setTimeout(resolve, 5));

    if (url.pathname === "/api/v1/chats/" && url.searchParams.get("page") === "1") {
      return json([
        {
          id: "chat-1",
          title: headers.get("Authorization") === "Bearer token-b" ? "B list" : "A list",
          provider: "gpt",
        },
      ]);
    }
    if (
      url.pathname === "/api/v1/chats/pinned" ||
      url.pathname === "/api/v1/chats/all/tags" ||
      url.pathname === "/api/v1/folders/" ||
      url.pathname === "/api/v1/folders/shared" ||
      url.pathname === "/api/v1/notes/pinned" ||
      url.pathname === "/api/v1/chats/chat-1/tags"
    ) {
      return json([]);
    }
    if (url.pathname === "/api/v1/turtle/chat/history/chat-1/initial") {
      return json({
        id: "chat-1",
        chat: {
          title: "Conversation",
          history: {
            marker: "private-message-body",
            currentId: "message-2",
            messages: {
              "message-2": {
                id: "message-2",
                parentId: "message-1",
                childrenIds: [],
                timestamp: 2,
                content: "newest",
              },
            },
            turtlePage: {
              rangeStart: 1,
              rangeEnd: 2,
              hasMore: true,
            },
          },
        },
      });
    }
    if (
      url.pathname === "/api/v1/turtle/chat/history/chat-1/range" &&
      url.searchParams.get("before_depth") === "1"
    ) {
      return json({
        messages: {
          "message-1": {
            id: "message-1",
            parentId: null,
            childrenIds: [],
            timestamp: 1,
            content: "older",
          },
        },
        page: {
          rangeStart: 0,
          rangeEnd: 1,
          hasMore: false,
        },
      });
    }
    if (url.pathname === "/api/v1/chats/chat-1") {
      return json({ ok: true });
    }
    if (url.pathname === "/api/tasks/chat/chat-1") {
      return json({ task_ids: [] });
    }
    return json({ ok: true });
  };

  const windowObject = {
    location: new URL("https://chat.example.test/"),
    fetch: nativeFetch,
    setTimeout,
    clearTimeout,
    addEventListener(type, listener) {
      const listeners = windowListeners.get(type) || [];
      listeners.push(listener);
      windowListeners.set(type, listeners);
    },
    dispatchEvent(event) {
      for (const listener of windowListeners.get(event.type) || []) listener(event);
      return true;
    },
  };
  const documentObject = {
    addEventListener(type, listener) {
      const listeners = documentListeners.get(type) || [];
      listeners.push(listener);
      documentListeners.set(type, listeners);
    },
  };
  const context = vm.createContext({
    window: windowObject,
    document: documentObject,
    localStorage,
    sessionStorage,
    crypto: webcrypto,
    Request,
    Response,
    Headers,
    URL,
    TextEncoder,
    Uint8Array,
    Element: FakeElement,
    CustomEvent: FakeCustomEvent,
    setTimeout,
    clearTimeout,
    console,
  });
  const source = await readFile(
    new URL("../../branding/open-webui/client-read-cache.js", import.meta.url),
    "utf8",
  );
  vm.runInContext(source, context, { filename: "client-read-cache.js" });
  return { windowObject, localStorage, sessionStorage, networkCalls };
};

test("sidebar and scoped conversation snapshots survive a same-tab reload", async () => {
  const { windowObject, localStorage, sessionStorage, networkCalls } = await createHarness();
  const authA = { Authorization: "Bearer token-a" };

  const [firstList, joinedList] = await Promise.all([
    windowObject.fetch("/api/v1/chats/?page=1", { headers: authA }),
    windowObject.fetch("/api/v1/chats/?page=1", { headers: authA }),
  ]);
  assert.equal((await firstList.json())[0].title, "A list");
  assert.equal((await joinedList.json())[0].title, "A list");
  assert.equal(
    networkCalls.filter((call) => call.path === "/api/v1/chats/?page=1").length,
    1,
  );

  const cachedList = await windowObject.fetch("/api/v1/chats/?page=1", {
    headers: authA,
  });
  assert.equal(cachedList.headers.get("X-Turtle-Client-Cache"), "hit");
  assert.equal(
    networkCalls.filter((call) => call.path === "/api/v1/chats/?page=1").length,
    1,
  );
  assert.ok(sessionStorage.length > 0);
  assert.ok(sessionStorage.values().every((value) => !value.includes("token-a")));

  await windowObject.__turtleClientReadCache.prefetchConversation("chat-1");
  const callsAfterPrefetch = networkCalls.length;
  const [chat, tags, tasks] = await Promise.all([
    windowObject.fetch("/api/v1/chats/chat-1", { headers: authA }),
    windowObject.fetch("/api/v1/chats/chat-1/tags", { headers: authA }),
    windowObject.fetch("/api/tasks/chat/chat-1", { headers: authA }),
  ]);
  const chatPayload = await chat.json();
  assert.equal(chatPayload.id, "chat-1");
  assert.deepEqual(await tags.json(), []);
  assert.deepEqual(await tasks.json(), { task_ids: [] });
  assert.equal(networkCalls.length, callsAfterPrefetch);
  assert.ok(sessionStorage.values().some((value) => value.includes("private-message-body")));
  assert.ok(sessionStorage.values().every((value) => !value.includes("token-a")));

  const [olderLoaded, olderJoined] = await Promise.all([
    windowObject.__turtleHistoryPager.loadOlder("chat-1", chatPayload.chat.history),
    windowObject.__turtleHistoryPager.loadOlder("chat-1", chatPayload.chat.history),
  ]);
  assert.equal(olderLoaded, true);
  assert.equal(olderJoined, true);
  assert.equal(
    networkCalls.filter(
      (call) =>
        call.path ===
        "/api/v1/turtle/chat/history/chat-1/range?before_depth=1",
    ).length,
    1,
  );
  assert.deepEqual(
    Array.from(chatPayload.chat.history.messages["message-1"].childrenIds),
    ["message-2"],
  );
  assert.equal(chatPayload.chat.history.turtlePage.hasMore, false);
  assert.equal(
    windowObject.__turtleClientReadCache.stats().historyPageSingleFlightJoins,
    1,
  );

  const strippedHistory = {
    currentId: "message-2",
    messages: {
      "message-2": {
        id: "message-2",
        parentId: "message-1",
        childrenIds: [],
        timestamp: 2,
        content: "newest",
      },
    },
  };
  const fallbackHarness = await createHarness();
  await fallbackHarness.windowObject.fetch("/api/v1/chats/chat-1", {
    headers: authA,
  });
  assert.equal(
    await fallbackHarness.windowObject.__turtleHistoryPager.loadOlder(
      "chat-1",
      strippedHistory,
    ),
    true,
  );
  assert.equal(strippedHistory.turtlePage.hasMore, false);
  assert.deepEqual(
    Array.from(strippedHistory.messages["message-1"].childrenIds),
    ["message-2"],
  );
  assert.equal(
    fallbackHarness.networkCalls.filter(
      (call) =>
        call.path ===
        "/api/v1/turtle/chat/history/chat-1/range?before_depth=1",
    ).length,
    1,
  );

  const callsBeforeReload = networkCalls.length;
  const reloaded = await createHarness({ localStorage, sessionStorage, networkCalls });
  const [reloadedChat, reloadedTags, reloadedTasks] = await Promise.all([
    reloaded.windowObject.fetch("/api/v1/chats/chat-1", { headers: authA }),
    reloaded.windowObject.fetch("/api/v1/chats/chat-1/tags", { headers: authA }),
    reloaded.windowObject.fetch("/api/tasks/chat/chat-1", { headers: authA }),
  ]);
  assert.equal(reloadedChat.headers.get("X-Turtle-Client-Cache"), "session");
  assert.equal(reloadedTags.headers.get("X-Turtle-Client-Cache"), "session");
  assert.equal(reloadedTasks.headers.get("X-Turtle-Client-Cache"), "session");
  assert.equal((await reloadedChat.json()).id, "chat-1");
  assert.deepEqual(await reloadedTags.json(), []);
  assert.deepEqual(await reloadedTasks.json(), { task_ids: [] });
  assert.equal(networkCalls.length, callsBeforeReload);

  await reloaded.windowObject.fetch("/api/v1/chats/chat-1", {
    method: "POST",
    headers: { ...authA, "Content-Type": "application/json" },
    body: "{}",
  });
  const callsBeforeInvalidatedRead = networkCalls.length;
  await reloaded.windowObject.fetch("/api/v1/chats/chat-1", { headers: authA });
  assert.equal(networkCalls.length, callsBeforeInvalidatedRead + 1);

  localStorage.setItem("token", "token-b");
  const switchedList = await reloaded.windowObject.fetch("/api/v1/chats/?page=1", {
    headers: { Authorization: "Bearer token-b" },
  });
  assert.equal((await switchedList.json())[0].title, "B list");
  assert.equal(
    networkCalls.filter((call) => call.path === "/api/v1/chats/?page=1").length,
    2,
  );
  assert.ok(sessionStorage.values().every((value) => !value.includes("A list")));
  assert.ok(
    sessionStorage.values().every((value) => !value.includes("private-message-body")),
  );

  const stats = windowObject.__turtleClientReadCache.stats();
  assert.ok(stats.sidebarHits >= 1);
  assert.ok(stats.sidebarSingleFlightJoins >= 1);
  assert.ok(stats.conversationHits >= 3);
  assert.ok(reloaded.windowObject.__turtleClientReadCache.stats().conversationSessionHits >= 3);
});

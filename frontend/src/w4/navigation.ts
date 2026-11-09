import type { Route, Section } from "./model";

export function sectionOf(route: Route): Section {
  switch (route.name) {
    case "chat":
    case "chats":
      return "chats";
    case "contacts":
    case "contact":
    case "requests":
      return "contacts";
    case "discover":
    case "discoverFeed":
    case "discoverMoments":
    case "discoverPeople":
    case "persona":
    case "events":
    case "moments":
      return "discover";
    case "expressions":
    case "me":
    case "settings":
      return "me";
  }
}

export function routeToHash(route: Route): string {
  switch (route.name) {
    case "chat": return `#/chat/${encodeURIComponent(route.friendId)}`;
    case "contact": return `#/contacts/${encodeURIComponent(route.friendId)}`;
    case "persona": return `#/persona/${encodeURIComponent(route.personaId)}`;
    case "settings": return route.section ? `#/settings/${encodeURIComponent(route.section)}` : "#/settings";
    case "discoverFeed":
    case "discoverMoments":
    case "moments": return "#/discover";
    case "discoverPeople": return "#/discover/people";
    case "events": return "#/events";
    default: return `#/${route.name}`;
  }
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  switch (parts[0]) {
    case "chat": return parts[1] ? { name: "chat", friendId: parts[1] } : { name: "chats" };
    case "contacts": return parts[1] ? { name: "contact", friendId: parts[1] } : { name: "contacts" };
    case "discover": return parts[1] === "people" ? { name: "discoverPeople" } : { name: "discover" };
    case "persona": return parts[1] ? { name: "persona", personaId: parts[1] } : { name: "discover" };
    case "events": return { name: "events" };
    case "expressions": return { name: "expressions" };
    case "me": return { name: "me" };
    case "settings": return { name: "settings", section: parts[1] };
    case "requests": return { name: "requests" };
    case "chats": return { name: "chats" };
    default: return { name: "chats" };
  }
}

export function backRoute(route: Route): Route {
  switch (route.name) {
    case "chat": return { name: "chats" };
    case "contacts": return { name: "chats" };
    case "contact": return { name: "contacts" };
    case "persona": return { name: "discover" };
    case "discoverPeople":
    case "events": return { name: "discover" };
    case "settings":
    case "expressions": return { name: "me" };
    case "requests": return { name: "contacts" };
    default: return route;
  }
}

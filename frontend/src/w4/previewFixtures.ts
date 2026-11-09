/**
 * Deterministic preview fixtures. Used only by /design-preview iframes.
 * These are never imported by a production path unless ?preview=1 is set.
 */

const NOW = "2026-11-09T12:00:00.000Z";

const AVATARS = ["callum", "giulia", "annika", "adrian", "camila", "finn", "haruto", "ines", "jiwoo", "lin", "mei", "niamh"];

const PEOPLE = [
  { name: "Callum", country: "United Kingdom", city: "London", job: "Product designer", lang: "en", age: 29, bio: "Coffee, small typography details and long walks by the canal." },
  { name: "Giulia", country: "Italy", city: "Milan", job: "Architect", lang: "en", age: 31, bio: "I collect old postcards and overthink restaurant menus." },
  { name: "Annika", country: "Germany", city: "Berlin", job: "Sound engineer", lang: "en", age: 27, bio: "Field recordings, flea markets, and very slow Sundays." },
  { name: "Adrian", country: "Spain", city: "Barcelona", job: "Chef", lang: "en", age: 33, bio: "I will always tell you the best place for breakfast." },
  { name: "Camila", country: "Brazil", city: "São Paulo", job: "Illustrator", lang: "en", age: 26, bio: "Drawing people on the metro and learning to surf badly." },
  { name: "Finn", country: "Ireland", city: "Dublin", job: "Bookseller", lang: "en", age: 30, bio: "Ask me for a book recommendation and clear your afternoon." },
  { name: "Haruto", country: "Japan", city: "Tokyo", job: "Ceramicist", lang: "en", age: 34, bio: "Quiet mornings, loud music, and a shelf full of mugs." },
  { name: "Ines", country: "Portugal", city: "Lisbon", job: "Marine biologist", lang: "en", age: 28, bio: "I work with tide pools and talk about them too much." },
  { name: "Jiwoo", country: "Korea", city: "Seoul", job: "Motion designer", lang: "en", age: 25, bio: "Making tiny animations and finding the best iced coffee." },
  { name: "Lin", country: "China", city: "Shanghai", job: "Editor", lang: "en", age: 32, bio: "I read three books at once and finish none of them." },
  { name: "Mei", country: "Singapore", city: "Singapore", job: "Urban planner", lang: "en", age: 30, bio: "Maps, hawker centres, and the perfect walking route." },
  { name: "Niamh", country: "Ireland", city: "Galway", job: "Radio producer", lang: "en", age: 27, bio: "I ask too many questions for a living." },
];

export const PREVIEW_PROFILE = {
  nickname: "小林",
  target_languages: ["en"],
  native_language: "zh",
  birthday: "1998-04-12",
  fact_sheet: {
    profile: { city: "上海", major_or_job: "产品设计师", gender: "female", level: "能聊几句" },
    interests: ["旅行", "音乐", "摄影", "咖啡"],
    language_profile: { levels: { en: "能聊几句" } },
  },
};

export function previewFriends(count = 12) {
  return Array.from({ length: count }, (_, index) => {
    const person = PEOPLE[index % PEOPLE.length];
    const id = index < PEOPLE.length ? `fixture-${index + 1}` : `fixture-${index + 1}`;
    return {
      id,
      name: index < PEOPLE.length ? person.name : `${person.name} ${Math.floor(index / PEOPLE.length) + 1}`,
      nationality: person.country,
      city: person.city,
      occupation: person.job,
      reply_language: person.lang,
      avatar_url: `/mock/avatars/${AVATARS[index % AVATARS.length]}.jpg`,
      avatar_seed: String(index + 1),
      gender: index % 2 ? "female" : "male",
      timezone: "Europe/London",
      age: person.age,
      bio: person.bio,
      total_turns: 12 + (index % 9),
      closeness_level: 2 + (index % 3),
      unread: index === 0 ? 2 : index === 1 ? 1 : 0,
      is_awake: index < 4,
      local_time: ["11:20", "12:05", "09:40", "18:15"][index % 4],
      voice_id: "ready-voice",
      last_interaction_at: new Date(Date.parse(NOW) - index * 37 * 60 * 1000).toISOString(),
      last_message: previewMessageFor(index, 0),
    };
  });
}

const TEXTS = [
  ["I found a tiny coffee place this morning. It had exactly four seats and the owner knew everyone's order.", "That sounds like a place worth protecting. What did you order?"],
  ["I finally finished the chair I was restoring. It only took me seven months.", "Seven months! Please tell me you're sitting on it right now."],
  ["It's raining again, so I'm pretending it's a valid reason to stay in and read.", "That is the most valid reason I've heard all week."],
  ["I made pasta for six people and then remembered I live alone.", "Leftovers are just meal prep with a story."],
  ["Today I saw a dog carrying a baguette. I have never been happier.", "I need a photograph of this immediately."],
  ["I'm trying to learn to cook one thing properly instead of ten things badly.", "That's a much better plan than mine."],
  ["I booked a train ticket with no plan for the other end.", "The best journeys start exactly like that."],
  ["My neighbour gave me too many tomatoes, so now I'm making sauce for the whole building.", "You've become the tomato distribution network."],
  ["I went to a record shop and came home with something I can't even play.", "That's how record shops work, I think."],
  ["I've started walking a different route to work and everything looks new.", "It's strange how much of a city is invisible on autopilot."],
  ["The exhibition was smaller than I expected, but one room was perfect.", "Sometimes one room is enough."],
  ["I'm reading a book about cities and now I keep looking at benches differently.", "You're going to be unbearable at dinner parties."],
];

export function previewMessageFor(friendIndex: number, index: number) {
  const texts = TEXTS[friendIndex % TEXTS.length];
  const isMe = index % 2 === 1;
  const text = isMe ? texts[1] : texts[0];
  return {
    id: `fixture-message-${friendIndex}-${index}`,
    friend_id: `fixture-${friendIndex + 1}`,
    sender: isMe ? "user" : "friend",
    msg_type: "text",
    transcript: text,
    created_at: new Date(Date.parse(NOW) + index * 60_000).toISOString(),
    is_read: true,
  };
}

export function previewThread(friendIndex: number, count = 12) {
  return Array.from({ length: count }, (_, index) => previewMessageFor(friendIndex, index));
}

export function previewConversations(friends: ReturnType<typeof previewFriends>) {
  return friends.map((friend, index) => ({
    friend_id: friend.id,
    last_message: previewMessageFor(index, 5),
    unread: friend.unread,
  }));
}

export const PREVIEW_EXPRESSIONS = [
  {
    id: "expr-1",
    original_text: "I very like this cafe.",
    better_expression: "I really like this cafe.",
    explanation: "Use “really” before a verb; “very” mostly describes adjectives.",
    status: "learning",
    mastery_count: 1,
    created_at: NOW,
    source_friend_id: "fixture-1",
  },
  {
    id: "expr-2",
    original_text: "I have ever been to Kyoto.",
    better_expression: "I've been to Kyoto before.",
    explanation: "“Have ever been” needs a question or a present-perfect frame; “before” is natural in a statement.",
    status: "learning",
    mastery_count: 2,
    created_at: NOW,
    source_friend_id: "fixture-3",
  },
  {
    id: "expr-3",
    original_text: "It is worth to visit.",
    better_expression: "It's worth visiting.",
    explanation: "“Worth” is followed by an -ing form.",
    status: "mastered",
    mastery_count: 3,
    created_at: NOW,
    source_friend_id: "fixture-2",
  },
];

export function previewFeed(count = 6) {
  return Array.from({ length: count }, (_, index) => {
    const person = PEOPLE[index % PEOPLE.length];
    const photo = ["cafe", "lisbon", "flowers", "market", "records", "beach", "tokyo", "desk", "brunch"][index % 9];
    return {
      kind: "persona_post",
      id: `fixture-post-${index + 1}`,
      content: [
        "A slow morning, good coffee, and no plans. Sometimes that's all you need.",
        "The light in the market this morning made everything look like a painting.",
        "I keep buying flowers for a table that only ever has one person at it.",
        "Found a record with a cover so beautiful I didn't check if it plays.",
        "A short walk that turned into a three-hour conversation with a stranger.",
        "The sea was so calm it looked like it was waiting for something.",
      ][index % 6],
      media: [`/mock/photos/${photo}.jpg`],
      alt_text: "A quiet everyday scene",
      media_credit: "SpeakEasy preview fixture",
      tags: [["daily", "coffee"], ["travel", "market"], ["flowers", "home"], ["music", "records"]][index % 4],
      language: "en",
      content_hash: `hash-${index}`,
      speech_version: "v1",
      visibility: "public",
      created_at: new Date(Date.parse(NOW) - index * 3_600_000).toISOString(),
      like_count: 3 + index,
      comment_count: index % 3,
      liked_by_me: index === 1,
      is_mine: false,
      friendship_status: "accepted",
      author: {
        id: `persona-${(index % PEOPLE.length) + 1}`,
        nickname: person.name,
        avatar: `/mock/avatars/${AVATARS[index % AVATARS.length]}.jpg`,
        bio: person.bio,
        interests: ["coffee", "design", "travel"],
        country: person.country,
        city: person.city,
        occupation: person.job,
        age: person.age,
        gender: index % 2 ? "female" : "male",
        language: "en",
        is_ai: true,
        ai_label: "AI",
      },
      comments: index % 3
        ? [
            {
              id: `fixture-comment-${index}`,
              content: "This made me want to slow down too.",
              created_at: NOW,
              author: { type: "persona", nickname: "Mei", persona_id: "persona-11", is_ai: true, ai_label: "AI" },
            },
          ]
        : [],
    };
  });
}

export function previewPersona(index = 0) {
  const person = PEOPLE[index % PEOPLE.length];
  return {
    id: `persona-${index + 1}`,
    nickname: person.name,
    avatar: `/mock/avatars/${AVATARS[index % AVATARS.length]}.jpg`,
    bio: person.bio,
    interests: ["coffee", "design", "walking", "records"],
    country: person.country,
    city: person.city,
    occupation: person.job,
    age: person.age,
    gender: index % 2 ? "female" : "male",
    language: "en",
    is_ai: true,
    ai_label: "AI",
    personality: "Observant, dryly funny, and genuinely curious about ordinary details.",
    timezone: "Europe/London",
    post_count: 4,
    posts: { items: previewFeed(4), next_cursor: "" },
    friendship: { id: "fixture-friendship", persona_id: `persona-${index + 1}`, status: "accepted", friend_id: `fixture-${index + 1}`, request_message: "", created_at: NOW, updated_at: NOW },
    relations: [],
  };
}

export function previewDiscoverNext() {
  const person = PEOPLE[4];
  return {
    persona: {
      id: "persona-5",
      nickname: person.name,
      avatar: `/mock/avatars/${AVATARS[4]}.jpg`,
      bio: person.bio,
      interests: ["illustration", "surfing", "street food"],
      country: person.country,
      city: person.city,
      occupation: person.job,
      age: person.age,
      gender: "female",
      language: "en",
      is_ai: true,
      ai_label: "AI",
    },
    greeting: "Hey! I'm Camila. I draw people on the metro and I'm learning to surf badly. What's something small that made your week better?",
    post_id: "fixture-post-5",
    post_hash: "hash-4",
    speech_version: "v1",
    match_tags: ["illustration", "coffee", "travel"],
  };
}

export function previewEvents() {
  return [
    {
      id: "fixture-event-1",
      type: "like",
      status: "done",
      is_read: false,
      created_at: NOW,
      scheduled_at: NOW,
      post_id: "fixture-post-1",
      payload: { persona_name: "Callum", post_excerpt: "A slow morning, good coffee…" },
      persona: { id: "persona-1", nickname: "Callum", avatar: `/mock/avatars/callum.jpg`, bio: "Coffee and conversations", is_ai: true, ai_label: "AI" },
    },
    {
      id: "fixture-event-2",
      type: "comment",
      status: "done",
      is_read: false,
      created_at: NOW,
      scheduled_at: NOW,
      post_id: "fixture-post-2",
      payload: { persona_name: "Giulia", comment: "This is exactly the kind of morning I needed to see.", post_excerpt: "The light in the market…" },
      persona: { id: "persona-2", nickname: "Giulia", avatar: `/mock/avatars/giulia.jpg`, bio: "Architect", is_ai: true, ai_label: "AI" },
    },
    {
      id: "fixture-event-3",
      type: "friend_request",
      status: "pending",
      is_read: false,
      created_at: NOW,
      scheduled_at: NOW,
      post_id: "",
      payload: { persona_name: "Ines", message: "Your note about tide pools made me smile. Want to trade small observations?" },
      persona: { id: "persona-8", nickname: "Ines", avatar: `/mock/avatars/ines.jpg`, bio: "Marine biologist", is_ai: true, ai_label: "AI" },
    },
  ];
}

export const PREVIEW_SETTINGS = {
  languages: ["en"],
  tts: { provider: "preview", voice: "Warm", speed: 1 },
  theme: "system",
};

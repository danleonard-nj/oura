(() => {
  "use strict";

  const jokes = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "A SQL query walks into a bar, sees two tables, and asks: “Can I join you?”",
    "Why did the developer go broke? Because he used up all his cache.",
    "How many programmers does it take to change a light bulb? None. That’s a hardware problem.",
    "There’s no place like 127.0.0.1.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why was the JavaScript developer sad? Because he didn’t know how to null his feelings.",
    "Programmer: “It works on my machine.”\nEveryone else: “Great, ship your machine.”",
    "AI walks into a bar. The bartender says, “What’ll you have?” AI says, “Whatever has the highest probability.”",
    "My code doesn’t have bugs. It just develops random features.",
  ];
  const joke = document.getElementById("joke");
  const count = document.getElementById("joke-count");
  const next = document.getElementById("next-joke");
  const random = document.getElementById("random-joke");
  let index = Math.floor(Math.random() * jokes.length);

  function render() {
    joke.textContent = jokes[index];
    count.textContent = `${String(index + 1).padStart(2, "0")} / ${jokes.length}`;
  }

  next.addEventListener("click", () => {
    index = (index + 1) % jokes.length;
    render();
  });
  random.addEventListener("click", () => {
    // Pick a different joke without retry loops or an immediate repeat.
    index = (index + 1 + Math.floor(Math.random() * (jokes.length - 1))) % jokes.length;
    render();
  });
  render();
  next.disabled = false;
  random.disabled = false;
})();

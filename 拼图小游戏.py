import tkinter as tk
from tkinter import messagebox
from heapq import heappush, heappop
import urllib.request
import json
import os
import random
from PIL import Image, ImageTk
import io

# Goal state: space represented by '_' at the end
GOAL = "12345678_"
GOAL_IDX = {ch: i for i, ch in enumerate(GOAL)}

# Four moves of the blank (delta in 1D index), with boundary restrictions
MOVES = {
    'U': -3,  # Up: row-1
    'D': +3,  # Down: row+1
    'L': -1,  # Left: col-1 (invalid at positions 0,3,6)
    'R': +1,  # Right: col+1 (invalid at positions 2,5,8)
}
MOVE_DESC = {
    'U': 'Up',
    'D': 'Down',
    'L': 'Left',
    'R': 'Right',
}
# Invalid positions for each direction
INVALID = {
    'L': [0, 3, 6],
    'R': [2, 5, 8],
    'U': [0, 1, 2],
    'D': [6, 7, 8],
}


def manhattan(state: str) -> int:
    """Manhattan distance heuristic h(state)"""
    dist = 0
    for idx, ch in enumerate(state):
        if ch != '_':
            gi = GOAL_IDX[ch]
            dist += abs(idx // 3 - gi // 3) + abs(idx % 3 - gi % 3)
    return dist


def solve(start: str):
    """
    Returns (min_steps, move_sequence) where sequence is a string like "ULDR..."
    Moves indicate the direction the blank moves.
    If unsolvable, returns (-1, "unsolvable").
    """
    if start == GOAL:
        return 0, ""

    # Unsolvable check: parity of inversions (ignore blank '_')
    tiles = [c for c in start if c != '_']
    inv = sum(1 for i in range(8) for j in range(i + 1, 8) if tiles[i] > tiles[j])
    if inv % 2 != 0:
        return -1, "unsolvable (parity mismatch)"

    visited = set()
    # heap element: (f = g+h, g, state, path_string)
    heap = [(manhattan(start), 0, start, "")]
    while heap:
        f, g, state, path = heappop(heap)
        if state == GOAL:
            return g, path
        if state in visited:
            continue
        visited.add(state)

        ei = state.index('_')
        for move, delta in MOVES.items():
            if ei in INVALID[move]:
                continue
            ni = ei + delta
            lst = list(state)
            lst[ei], lst[ni] = lst[ni], lst[ei]
            nxt = ''.join(lst)
            if nxt not in visited:
                ng = g + 1
                heappush(heap, (ng + manhattan(nxt), ng, nxt, path + move))

    return -1, "unsolvable"


class BingImageFetcher:
    """Fetch Bing daily image and return as PIL Image (square cropped, resized to 300x300)."""
    CACHE_DIR = ".bing_cache"
    CACHE_FILE = os.path.join(CACHE_DIR, "bing_image.jpg")

    @classmethod
    def fetch(cls, idx=0):
        """idx: which image from the daily collection (0 = primary)"""
        os.makedirs(cls.CACHE_DIR, exist_ok=True)
        try:
            # Get image metadata
            url = f"https://www.bing.com/HPImageArchive.aspx?format=js&idx={idx}&n=1&mkt=en-US"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            img_url = "https://www.bing.com" + data['images'][0]['url']

            # Download image
            req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                img_data = resp.read()

            # Save cache
            with open(cls.CACHE_FILE, 'wb') as f:
                f.write(img_data)

            # Process image
            pil_img = Image.open(io.BytesIO(img_data))
            # Crop to square (center)
            w, h = pil_img.size
            size = min(w, h)
            left = (w - size) // 2
            top = (h - size) // 2
            pil_img = pil_img.crop((left, top, left + size, top + size))
            pil_img = pil_img.resize((300, 300), Image.LANCZOS)
            return pil_img
        except Exception as e:
            print(f"Failed to fetch Bing image: {e}")
            return None

    @classmethod
    def get_cached(cls):
        if os.path.exists(cls.CACHE_FILE):
            try:
                pil_img = Image.open(cls.CACHE_FILE)
                w, h = pil_img.size
                size = min(w, h)
                left = (w - size) // 2
                top = (h - size) // 2
                pil_img = pil_img.crop((left, top, left + size, top + size))
                pil_img = pil_img.resize((300, 300), Image.LANCZOS)
                return pil_img
            except:
                pass
        return None


class EightPuzzleVisualizer:
    """Eight-puzzle visualizer with step-by-step animation and custom initial state input."""

    def __init__(self, master=None):
        self.master = master or tk.Tk()
        self.master.title("8-Puzzle Solver Visualizer")
        self.master.geometry("540x720")
        self.master.resizable(False, False)

        # Data attributes
        self.initial_state = ""
        self.current_step = 0
        self.state_history = []
        self.auto_timer = None
        self.use_bg = tk.BooleanVar(value=False)
        self.bg_image = None  # PIL Image (300x300)
        self.tile_photos = {}  # dict: char -> PhotoImage
        self._bg_photo = None  # keep reference for display

        # Build UI
        self._build_top_input()
        self._build_board_section()
        self._build_control_section()
        self._build_background_options()

        # Bind canvas click
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        # Default initial state (example)
        self.set_initial_state("8_2467351")

    # ---------- UI Construction ----------
    def _build_top_input(self):
        top = tk.Frame(self.master)
        top.pack(pady=(12, 4), fill='x', padx=10)

        tk.Label(top, text="State:", font=('Segoe UI', 12)).grid(row=0, column=0, padx=(0, 4))

        self.input_entry = tk.Entry(top, width=12, font=('Consolas', 12))
        self.input_entry.grid(row=0, column=1, padx=4)
        self.input_entry.insert(0, "8_2467351")

        self.set_btn = tk.Button(top, text="Set", command=self.on_set_state,
                                 font=('Segoe UI', 12), padx=10, pady=2)
        self.set_btn.grid(row=0, column=2, padx=4)

        tk.Label(top, text="(e.g. 12345678_)", font=('Segoe UI', 12), fg='gray').grid(row=0, column=3, padx=4)

    def _build_board_section(self):
        self.canvas = tk.Canvas(self.master, width=300, height=300, bg='white')
        self.canvas.pack(pady=10)

        self.info_label = tk.Label(self.master, text="", font=('Segoe UI', 13))
        self.info_label.pack(pady=5)

    def _build_control_section(self):
        control_frame = tk.Frame(self.master)
        control_frame.pack(pady=5)

        self.prev_btn = tk.Button(control_frame, text="Previous", command=self.prev_step,
                                  font=('Segoe UI', 11), padx=8)
        self.prev_btn.grid(row=0, column=0, padx=5)

        self.next_btn = tk.Button(control_frame, text="Next", command=self.next_step,
                                  font=('Segoe UI', 11), padx=8)
        self.next_btn.grid(row=0, column=1, padx=5)

        self.reset_btn = tk.Button(control_frame, text="Reset", command=self.reset,
                                   font=('Segoe UI', 11), padx=8)
        self.reset_btn.grid(row=0, column=2, padx=5)

        # Auto play section
        auto_frame = tk.Frame(self.master)
        auto_frame.pack(pady=6)

        self.auto_btn = tk.Button(auto_frame, text="Auto Play", command=self.toggle_auto_play,
                                  font=('Segoe UI', 11), bg='lightblue')
        self.auto_btn.pack(side=tk.LEFT, padx=5)

        tk.Label(auto_frame, text="Speed (sec/step):", font=('Segoe UI', 10)).pack(side=tk.LEFT)
        self.speed_var = tk.StringVar(value="1.0")
        speed_spinbox = tk.Spinbox(auto_frame, from_=0.1, to=3.0, increment=0.1,
                                   textvariable=self.speed_var, width=5,
                                   font=('Segoe UI', 10))
        speed_spinbox.pack(side=tk.LEFT, padx=5)

        # Shuffle button
        shuffle_frame = tk.Frame(self.master)
        shuffle_frame.pack(pady=4)
        self.shuffle_btn = tk.Button(shuffle_frame, text="🎲 Shuffle", command=self.shuffle_puzzle,
                                     font=('Segoe UI', 11), bg='lightgreen')
        self.shuffle_btn.pack()

        # Keyboard shortcuts
        self.master.bind('<Right>', lambda e: self.next_step())
        self.master.bind('<Left>', lambda e: self.prev_step())
        self.master.bind('<space>', lambda e: self.toggle_auto_play())

    def _build_background_options(self):
        bg_frame = tk.Frame(self.master)
        bg_frame.pack(pady=6)

        self.bg_check = tk.Checkbutton(bg_frame, text="Use Bing daily image as background",
                                       variable=self.use_bg, command=self._toggle_bg)
        self.bg_check.pack(side=tk.LEFT, padx=5)

        self.fetch_btn = tk.Button(bg_frame, text="↻ Fetch New", command=self._fetch_new_bg,
                                   font=('Segoe UI', 10))
        self.fetch_btn.pack(side=tk.LEFT, padx=5)

    # ---------- Background handling ----------
    def _load_bg_image(self):
        """Try to load cached Bing image; if fails, fallback to None."""
        pil = BingImageFetcher.get_cached()
        if pil is None:
            pil = BingImageFetcher.fetch()
        if pil:
            self.bg_image = pil
            self._generate_tile_photos()
        else:
            self.bg_image = None
            self.tile_photos.clear()

    def _generate_tile_photos(self):
        """Cut the 300x300 bg image into 9 tiles (100x100) and map each to its goal position char."""
        if self.bg_image is None:
            self.tile_photos.clear()
            return
        photos = {}
        for idx, ch in enumerate(['1', '2', '3', '4', '5', '6', '7', '8', '_']):
            goal_pos = GOAL_IDX[ch]  # where this tile should end up
            row = goal_pos // 3
            col = goal_pos % 3
            crop = self.bg_image.crop((col * 100, row * 100, (col + 1) * 100, (row + 1) * 100))
            photo = ImageTk.PhotoImage(crop)
            photos[ch] = photo
        self.tile_photos = photos

    def _toggle_bg(self):
        if self.use_bg.get():
            self._load_bg_image()
        else:
            self.tile_photos.clear()
        if self.state_history:
            self.draw_state(self.state_history[self.current_step])

    def _fetch_new_bg(self):
        """Fetch a new Bing image (index 0) and refresh display."""
        pil = BingImageFetcher.fetch(idx=random.randint(0, 7))  # try different images
        if pil:
            self.bg_image = pil
            self._generate_tile_photos()
            if self.state_history:
                self.draw_state(self.state_history[self.current_step])
        else:
            messagebox.showerror("Network Error", "Failed to fetch Bing image.")

    # ---------- Core Logic ----------
    def validate_state(self, s: str) -> bool:
        """Check if s is a valid 8-puzzle state."""
        if len(s) != 9:
            return False
        allowed = set('12345678_')
        if any(c not in allowed for c in s):
            return False
        counts = {}
        for c in s:
            counts[c] = counts.get(c, 0) + 1
        if counts.get('_', 0) != 1:
            return False
        for d in '12345678':
            if counts.get(d, 0) != 1:
                return False
        return True

    def on_set_state(self):
        """Called when user clicks Set button."""
        new_state = self.input_entry.get().strip()
        if not self.validate_state(new_state):
            messagebox.showerror("Invalid Input",
                                 "State must be 9 characters containing digits 1-8 "
                                 "and one underscore '_'.\nExample: 12345678_")
            return
        self.set_initial_state(new_state)

    def set_initial_state(self, state: str):
        """Solve puzzle and initialize visualization."""
        self.initial_state = state
        self.stop_auto_play()

        steps, path = solve(state)
        if steps == -1:
            messagebox.showwarning("Unsolvable",
                                   "This puzzle configuration has no solution.\n"
                                   "Please try a different initial state.")
            self.state_history = []
            self.current_step = 0
            self.canvas.delete("all")
            self.info_label.config(text="No solution")
            self._update_button_states()
            return

        # Generate all intermediate states (full solution path)
        self.state_history = [state]
        cur = state
        for move in path:
            ei = cur.index('_')
            delta = MOVES[move]
            ni = ei + delta
            lst = list(cur)
            lst[ei], lst[ni] = lst[ni], lst[ei]
            cur = ''.join(lst)
            self.state_history.append(cur)

        self.current_step = 0
        self.draw_state(self.state_history[0])
        self._update_info()
        self._update_button_states()

    def draw_state(self, state: str):
        """Draw the given state on canvas."""
        self.canvas.delete("all")
        cell_size = 100

        # Colors for non-background mode
        colors = {
            '1': '#FF6B6B', '2': '#4ECDC4', '3': '#45B7D1',
            '4': '#96CEB4', '5': '#FFEAA7', '6': '#DDA0DD',
            '7': '#98D8C8', '8': '#F7DC6F', '_': '#FFFFFF'
        }

        for i, ch in enumerate(state):
            row = i // 3
            col = i % 3
            x1 = col * cell_size
            y1 = row * cell_size
            x2 = x1 + cell_size
            y2 = y1 + cell_size

            if self.use_bg.get() and ch in self.tile_photos:
                # Use image tile
                self.canvas.create_image(x1, y1, anchor='nw', image=self.tile_photos[ch])
                # Draw border
                self.canvas.create_rectangle(x1, y1, x2, y2, outline='#333', width=2)
                # Overlay small white number for identification
                if ch != '_':
                    # Semi-transparent background for number
                    self.canvas.create_rectangle(x1 + 2, y1 + 2, x1 + 26, y1 + 22, fill='white', outline='')
                    self.canvas.create_text(x1 + 14, y1 + 12, text=ch, font=('Arial', 12, 'bold'), fill='black')
            else:
                # Draw colored rectangle
                color = colors.get(ch, '#FFFFFF')
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline='#333', width=2)
                if ch != '_':
                    self.canvas.create_text(
                        (x1 + x2) / 2, (y1 + y2) / 2,
                        text=ch, font=('Arial', 28, 'bold'),
                        fill='#2C3E50'
                    )

        # Highlight blank position with red dashed circle
        blank_idx = state.index('_')
        row = blank_idx // 3
        col = blank_idx % 3
        cx = col * cell_size + cell_size // 2
        cy = row * cell_size + cell_size // 2
        r = cell_size // 2 - 6
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                outline='red', width=3, dash=(5, 3))

    def _is_adjacent(self, idx1, idx2):
        """Check if two indices are adjacent (up/down/left/right) on the 3x3 grid."""
        r1, c1 = divmod(idx1, 3)
        r2, c2 = divmod(idx2, 3)
        return abs(r1 - r2) + abs(c1 - c2) == 1

    def on_canvas_click(self, event):
        """Handle click on canvas – always active."""
        if not self.state_history:
            return

        cell_size = 100
        col = event.x // cell_size
        row = event.y // cell_size
        if col < 0 or col > 2 or row < 0 or row > 2:
            return
        clicked_idx = row * 3 + col

        current_state = self.state_history[self.current_step]
        blank_idx = current_state.index('_')

        if not self._is_adjacent(clicked_idx, blank_idx):
            return

        # Stop auto-play if running
        self.stop_auto_play()

        # Perform swap
        lst = list(current_state)
        lst[blank_idx], lst[clicked_idx] = lst[clicked_idx], lst[blank_idx]
        new_state = ''.join(lst)

        # Update history (truncate future, append new state)
        self.state_history = self.state_history[:self.current_step + 1]
        self.state_history.append(new_state)
        self.current_step += 1

        self.draw_state(new_state)
        self._update_info()
        self._update_button_states()

        # Check completion
        if new_state == GOAL:
            messagebox.showinfo("Congratulations!", "You solved the puzzle!")

    def _get_next_move_from(self, state):
        """Return the first move (character) to go from state towards goal, or '' if already goal."""
        if state == GOAL:
            return ''
        steps, path = solve(state)
        if steps == -1 or not path:
            return ''
        return path[0]

    def next_step(self):
        """Move forward one step. If at end, compute next optimal move dynamically."""
        total = len(self.state_history) - 1
        if self.current_step < total:
            # There are precomputed steps ahead
            self.current_step += 1
            self.draw_state(self.state_history[self.current_step])
            self._update_info()
            self._update_button_states()
            return

        # At the end, need to compute next step dynamically
        current_state = self.state_history[-1]
        if current_state == GOAL:
            messagebox.showinfo("Done", "Puzzle already solved!")
            return

        first_move = self._get_next_move_from(current_state)
        if not first_move:
            messagebox.showerror("Error", "Cannot find a solution from this state.")
            return

        # Execute first move
        ei = current_state.index('_')
        delta = MOVES[first_move]
        ni = ei + delta
        lst = list(current_state)
        lst[ei], lst[ni] = lst[ni], lst[ei]
        next_state = ''.join(lst)

        # Append to history
        self.state_history.append(next_state)
        self.current_step += 1
        self.draw_state(next_state)
        self._update_info()
        self._update_button_states()

    def prev_step(self):
        """Move backward one step."""
        if self.current_step > 0:
            self.current_step -= 1
            self.draw_state(self.state_history[self.current_step])
            self._update_info()
            self._update_button_states()

    def reset(self):
        """Reset to initial state."""
        self.stop_auto_play()
        if self.state_history:
            self.current_step = 0
            self.draw_state(self.state_history[0])
            self._update_info()
            self._update_button_states()

    def shuffle_puzzle(self):
        """Generate a random solvable puzzle by making random moves from goal."""
        state = GOAL
        num_moves = random.randint(25, 55)
        for _ in range(num_moves):
            ei = state.index('_')
            possible = []
            for move, delta in MOVES.items():
                if ei not in INVALID[move]:
                    ni = ei + delta
                    possible.append((move, ni))
            if not possible:
                continue
            move, ni = random.choice(possible)
            lst = list(state)
            lst[ei], lst[ni] = lst[ni], lst[ei]
            state = ''.join(lst)
        # Ensure it's not already solved
        if state == GOAL:
            state = "12345687_"  # fallback
        self.input_entry.delete(0, tk.END)
        self.input_entry.insert(0, state)
        self.set_initial_state(state)

    def _update_info(self):
        """Update the info label with current step and move description."""
        total = len(self.state_history) - 1
        cur = self.current_step
        if cur < total:
            # We have a precomputed next state; derive move description from difference
            current_state = self.state_history[cur]
            next_state = self.state_history[cur + 1]
            # Find which direction the blank moved
            ei = current_state.index('_')
            ni = next_state.index('_')
            delta = ni - ei
            move_map = {v: k for k, v in MOVES.items()}
            move_char = move_map.get(delta, '')
            desc = MOVE_DESC.get(move_char, '')
            text = f"Step {cur}/{total}"
            if desc:
                text += f"  → Blank moves {desc}"
        else:
            if self.state_history and self.state_history[-1] == GOAL:
                text = f"Solved! Total steps: {total}"
            else:
                # Show hint for next move
                next_move = self._get_next_move_from(self.state_history[-1]) if self.state_history else ''
                desc = MOVE_DESC.get(next_move, '')
                text = f"Step {cur}/{total}"
                if desc:
                    text += f"  → Hint: blank moves {desc}"
                else:
                    text += "  (Click a tile next to blank)"
        self.info_label.config(text=text)

    def _update_button_states(self):
        """Enable/disable navigation buttons based on current step."""
        total = len(self.state_history) - 1
        self.prev_btn.config(state=tk.NORMAL if self.current_step > 0 else tk.DISABLED)
        # Next enabled if there are more steps OR current state is not goal
        has_future = self.current_step < total
        is_not_goal = self.state_history and self.state_history[-1] != GOAL
        self.next_btn.config(state=tk.NORMAL if has_future or is_not_goal else tk.DISABLED)

    def toggle_auto_play(self):
        """Toggle auto-play on/off."""
        if self.auto_timer:
            self.stop_auto_play()
        else:
            self.start_auto_play()

    def start_auto_play(self):
        """Begin automatic step-by-step playback."""
        # If at end, try to compute next step first
        if self.current_step >= len(self.state_history) - 1:
            self.next_step()
            if self.current_step >= len(self.state_history) - 1:
                return  # still at end (maybe goal reached)
        self.auto_btn.config(text="Pause", bg='lightcoral')
        self._auto_step()

    def stop_auto_play(self):
        """Stop automatic playback."""
        if self.auto_timer:
            self.master.after_cancel(self.auto_timer)
            self.auto_timer = None
        self.auto_btn.config(text="Auto Play", bg='lightblue')

    def _auto_step(self):
        """Perform one step during auto-play."""
        if self.current_step >= len(self.state_history) - 1:
            # Try to compute next step
            self.next_step()
            if self.current_step >= len(self.state_history) - 1:
                self.stop_auto_play()
                return
        else:
            self.next_step()
        try:
            delay = float(self.speed_var.get()) * 750  # ms
        except ValueError:
            delay = 750
        self.auto_timer = self.master.after(int(delay), self._auto_step)

    def run(self):
        """Start the Tkinter main loop."""
        self.master.mainloop()


if __name__ == "__main__":
    app = EightPuzzleVisualizer()
    app.run()
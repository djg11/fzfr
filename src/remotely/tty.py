"""remotely.tty -- TTY prompt helper.

Opens /dev/tty directly so interactive prompts work even when fzf has
redirected both stdin and stdout for its own UI.
"""


def _tty_prompt(prompt_text):
    # type: (str) -> Optional[str]
    """Display a prompt on the terminal and return the user's input.

    Opens /dev/tty directly so the prompt works even when fzf has redirected
    stdin and stdout for its own UI. Clears the screen before showing the
    prompt so the fzf UI does not visually interfere with the input line.

    Returns the stripped input string, or None if /dev/tty is unavailable
    (e.g. Docker containers without a TTY, CI runners, nested fzf sessions).

    DESIGN: Centralises all tty interaction so any future prompt (extension
            filter, search query, rename, etc.) reuses the same clear-and-ask
            pattern without duplicating tty open/close or error handling.
    """
    try:
        with open("/dev/tty", "w") as tty_out:
            tty_out.write("\033[2J\033[H")
            tty_out.write(prompt_text)
            tty_out.flush()
        with open("/dev/tty") as tty_in:
            return tty_in.readline().strip()
    except OSError:
        return None

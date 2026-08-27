import tkinter as tk


def toggle_heart():
    if heart_button["text"] == "♡":
        heart_button.config(text="♥", fg="red")
    else:
        heart_button.config(text="♡", fg="black")


root = tk.Tk()
root.title("Heart Button")

heart_button = tk.Button(
    root,
    text="♡",              # Empty heart
    font=("Arial", 30),
    fg="black",
    borderwidth=0,
    command=toggle_heart
)

heart_button.pack(padx=30, pady=30)

root.mainloop()
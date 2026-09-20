"""No-key demonstration for the Day 4 checkpoint project."""
import os, tempfile
from scripts._term import CYAN, GREEN, RESET, print_step

def main():
    tmp=tempfile.mkdtemp(prefix="lab-demo-")
    os.environ["AGENT_DB"]=os.path.join(tmp,"agent.db"); os.environ["LAB_DB"]=os.path.join(tmp,"lab.db")
    from app.config import open_stores, make_providers
    from app.worker import Worker
    store,db=open_stores(); providers=make_providers(mock=True)
    questions=[("22CS045","Show me an oscilloscope and check whether I am trained for it."),
               ("22CS045","Book equipment 2 for 2026-09-19 14:00 and send me a confirmation.")]
    for roll,text in questions:
        thread=store.create_thread(roll); run_id=store.enqueue(thread,text,"mock")
        print(f"{CYAN}{roll}>{RESET} {text}")
        Worker(store,db,providers,worker_id="demo",on_step=print_step).run_until_idle()
        print(f"{GREEN}assistant>{RESET} {store.load_history(thread)[-1]['text']}\n")
    print("PASS: scripted demo completed")
if __name__=="__main__": main()

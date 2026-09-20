"""Day 4 multi-agent design for Lab Equipment Booking."""
import time
from collections.abc import Callable
from app.idempotency import idempotency_key
from app.lab_db import LabDb
from app.providers import AgentError
from app.tools.lab_tools import EquipmentTools, BookingTools, Toolset

SPECIALIST_MAX_STEPS = 6
SUPERVISOR_SYSTEM = """You are the Lab Equipment Assistant for student {roll_no}. Delegate discovery and
training questions to ask_equipment. Delegate bookings, returns and confirmation messages to ask_booking.
Give specialists complete requests including equipment ids and time slots. Answer briefly from their reports."""
EQUIPMENT_SYSTEM = """You are the read-only equipment specialist. Find equipment and check required training.
You cannot book, return or send notifications. Be brief and include equipment ids."""
BOOKING_SYSTEM = """You are the booking specialist for student {roll_no} only. Book or return equipment only
when requested. Trust the tools to enforce training, booking limits and slot clashes. Send a confirmation after
successful changes. Be brief."""


def run_tool(toolset: Toolset, db: LabDb, key: str, name: str, args: dict):
    try:
        if name in toolset.DELEGATES:
            return toolset.delegate(name, args, key), False
        if name in toolset.SIDE_EFFECTS:
            result, fresh = db.once(key, name, lambda: toolset.call(name, args))
            return result, not fresh
        return toolset.call(name, args), False
    except AgentError: raise
    except Exception as e:
        return {"error": "tool_failed", "hint": f"{name} failed ({type(e).__name__})."}, False


def run_specialist(agent, system, toolset, *, db, provider, task, parent_key, on_step=None):
    contents=[{"role":"user","text":task}]; functions=list(toolset.functions().values()); used=[]; seq=0
    while seq < SPECIALIST_MAX_STEPS:
        turn=provider.generate(system, contents, functions); seq += 1
        if not turn.tool_calls:
            return {"agent":agent,"answer":turn.text or "","tools_used":used}
        contents.append({"role":"model","text":turn.text,"raw":turn.raw,
                         "tool_calls":[{"name":c.name,"args":c.args} for c in turn.tool_calls]})
        for call in turn.tool_calls:
            seq += 1; key=idempotency_key(parent_key, seq, call.name, call.args); started=time.perf_counter()
            result,replayed=run_tool(toolset,db,key,call.name,call.args); used.append(call.name)
            if on_step: on_step({"agent":agent,"kind":"tool","tool":call.name,"args":call.args,"result":result,
                                 "ok":"error" not in result,"replayed":replayed,
                                 "ms":round((time.perf_counter()-started)*1000)})
            contents.append({"role":"tool","name":call.name,"result":result})
    return {"agent":agent,"error":"specialist_step_limit","tools_used":used}


class SupervisorTools(Toolset):
    TOOL_NAMES=("ask_equipment","ask_booking")
    DELEGATES=TOOL_NAMES
    def __init__(self, db, providers, roll_no, on_step=None):
        self.db,self.providers,self.roll_no,self.on_step=db,providers,roll_no,on_step
    def ask_equipment(self, question: str) -> dict:
        """Ask the read-only equipment specialist to list items or check training eligibility.

        Use for discovery and eligibility only; it cannot change data. Give a complete question including
        an equipment id when known. Returns the specialist's answer and tools used.
        """
        raise RuntimeError("delegations run through delegate()")
    def ask_booking(self, request: str) -> dict:
        """Ask the booking specialist to create/return a booking or send a confirmation. CAN CHANGE DATA.

        Use only for the current student's requested action. Include equipment id and exact slot for a booking.
        Returns the specialist's answer and tools used.
        """
        raise RuntimeError("delegations run through delegate()")
    def delegate(self,name,args,key):
        field="question" if name=="ask_equipment" else "request"
        if set(args)!={field} or not isinstance(args[field],str) or not args[field].strip():
            return {"error":"invalid_arguments","hint":f"{name} takes one non-empty string: {field}."}
        if self.on_step: self.on_step({"agent":"supervisor","kind":"delegate","tool":name,"args":args})
        if name=="ask_equipment":
            return run_specialist("equipment",EQUIPMENT_SYSTEM,EquipmentTools(self.db,self.roll_no),db=self.db,
                                  provider=self.providers["equipment"],task=args[field],parent_key=key,on_step=self.on_step)
        return run_specialist("booking",BOOKING_SYSTEM.format(roll_no=self.roll_no),BookingTools(self.db,self.roll_no),
                              db=self.db,provider=self.providers["booking"],task=args[field],parent_key=key,on_step=self.on_step)

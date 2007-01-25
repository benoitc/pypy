"""Register allocation.

"""

from pypy.rlib.objectmodel import we_are_translated
from pypy.rpython.lltypesystem import lltype
from pypy.jit.codegen.i386.operation import *


class StackOpCache:
    INITIAL_STACK_EBP_OFS = -4
stack_op_cache = StackOpCache()
stack_op_cache.lst = []

def stack_op(n):
    "Return the mem operand that designates the nth stack-spilled location"
    assert n >= 0
    lst = stack_op_cache.lst
    while len(lst) <= n:
        ofs = WORD * (StackOpCache.INITIAL_STACK_EBP_OFS - len(lst))
        lst.append(mem(ebp, ofs))
    return lst[n]

def stack_n_from_op(op):
    ofs = op.ofs_relative_to_ebp()
    return StackOpCache.INITIAL_STACK_EBP_OFS - ofs / WORD


class RegAllocator(object):
    AVAILABLE_REGS = [eax, edx, ebx, esi, edi]   # XXX ecx reserved for stuff

    # 'gv' -- GenVars, used as arguments and results of operations
    #
    # 'loc' -- location, a small integer that represents an abstract
    #          register number
    #
    # 'operand' -- a concrete machine code operand, which can be a
    #              register (ri386.eax, etc.) or a stack memory operand

    def __init__(self):
        self.nextloc = 0
        self.var2loc = {}
        self.available_locs = []
        self.force_loc2operand = {}
        self.force_operand2loc = {}
        self.initial_moves = []

    def set_final(self, final_vars_gv):
        for v in final_vars_gv:
            if not v.is_const and v not in self.var2loc:
                self.var2loc[v] = self.nextloc
                self.nextloc += 1

    def creating(self, v):
        try:
            loc = self.var2loc[v]
        except KeyError:
            pass
        else:
            self.available_locs.append(loc)   # now available again for reuse

    def using(self, v):
        if not v.is_const and v not in self.var2loc:
            try:
                loc = self.available_locs.pop()
            except IndexError:
                loc = self.nextloc
                self.nextloc += 1
            self.var2loc[v] = loc

    def creating_cc(self, v):
        if self.need_var_in_cc is v:
            # common case: v is a compare operation whose result is precisely
            # what we need to be in the CC
            self.need_var_in_cc = None
        self.creating(v)

    def save_cc(self):
        # we need a value to be in the CC, but we see a clobbering
        # operation, so we copy the original CC-creating operation down
        # past the clobbering operation
        v = self.need_var_in_cc
        if not we_are_translated():
            assert v in self.operations[:self.operationindex]
        self.operations.insert(self.operationindex, v)
        self.need_var_in_cc = None

    def using_cc(self, v):
        assert isinstance(v, Operation)
        assert 0 <= v.cc_result < INSN_JMP
        if self.need_var_in_cc is not None:
            self.save_cc()
        self.need_var_in_cc = v

    def allocate_locations(self, operations):
        # assign locations to gvars
        self.operations = operations
        self.need_var_in_cc = None
        self.operationindex = len(operations)
        for i in range(len(operations)-1, -1, -1):
            v = operations[i]
            kind = v.result_kind
            if kind == RK_WORD:
                self.creating(v)
            elif kind == RK_CC:
                self.creating_cc(v)
            if self.need_var_in_cc is not None and v.clobbers_cc:
                self.save_cc()
            v.allocate(self)
            self.operationindex = i
        if self.need_var_in_cc is not None:
            self.save_cc()

    def force_var_operands(self, force_vars, force_operands, at_start):
        force_loc2operand = self.force_loc2operand
        force_operand2loc = self.force_operand2loc
        for i in range(len(force_vars)):
            v = force_vars[i]
            operand = force_operands[i]
            try:
                loc = self.var2loc[v]
            except KeyError:
                if at_start:
                    pass    # input variable not used anyway
                else:
                    self.add_final_move(v, operand, make_copy=v.is_const)
            else:
                if loc in force_loc2operand or operand in force_operand2loc:
                    if at_start:
                        self.initial_moves.append((loc, operand))
                    else:
                        self.add_final_move(v, operand, make_copy=True)
                else:
                    force_loc2operand[loc] = operand
                    force_operand2loc[operand] = loc

    def add_final_move(self, v, targetoperand, make_copy):
        if make_copy:
            v = OpSameAs(v)
            self.operations.append(v)
        loc = self.nextloc
        self.nextloc += 1
        self.var2loc[v] = loc
        self.force_loc2operand[loc] = targetoperand

    def allocate_registers(self):
        # assign registers to locations that don't have one already
        force_loc2operand = self.force_loc2operand
        operands = []
        seen_regs = 0
        seen_stackn = {}
        for op in force_loc2operand.values():
            if isinstance(op, REG):
                seen_regs |= 1 << op.op
            elif isinstance(op, MODRM):
                seen_stackn[stack_n_from_op(op)] = None
        i = 0
        stackn = 0
        for loc in range(self.nextloc):
            try:
                operand = force_loc2operand[loc]
            except KeyError:
                # grab the next free register
                try:
                    while True:
                        operand = RegAllocator.AVAILABLE_REGS[i]
                        i += 1
                        if not (seen_regs & (1 << operand.op)):
                            break
                except IndexError:
                    while stackn in seen_stackn:
                        stackn += 1
                    operand = stack_op(stackn)
                    stackn += 1
            operands.append(operand)
        self.operands = operands
        self.required_frame_depth = stackn

    def get_operand(self, gv_source):
        if gv_source.is_const:
            return imm(gv_source.revealconst(lltype.Signed))
        else:
            loc = self.var2loc[gv_source]
            return self.operands[loc]

    def load_location_with(self, loc, gv_source):
        dstop = self.operands[loc]
        srcop = self.get_operand(gv_source)
        if srcop != dstop:
            self.mc.MOV(dstop, srcop)
        return dstop

    def generate_initial_moves(self):
        initial_moves = self.initial_moves
        # first make sure that the reserved stack frame is big enough
        last_n = self.required_frame_depth - 1
        for loc, srcoperand in initial_moves:
            if isinstance(srcoperand, MODRM):
                n = stack_n_from_op(srcoperand)
                if last_n < n:
                    last_n = n
        if last_n >= 0:
            if CALL_ALIGN > 1:
                last_n = (last_n & ~(CALL_ALIGN-1)) + (CALL_ALIGN-1)
            self.required_frame_depth = last_n + 1
            self.mc.LEA(esp, stack_op(last_n))
        # XXX naive algo for now
        for loc, srcoperand in initial_moves:
            if self.operands[loc] != srcoperand:
                self.mc.PUSH(srcoperand)
        initial_moves.reverse()
        for loc, srcoperand in initial_moves:
            if self.operands[loc] != srcoperand:
                self.mc.POP(self.operands[loc])

    def generate_operations(self):
        for v in self.operations:
            v.generate(self)
            cc = v.cc_result
            if cc >= 0 and v in self.var2loc:
                # force a comparison instruction's result into a
                # regular location
                dstop = self.get_operand(v)
                mc = self.mc
                insn = EMIT_SETCOND[cc]
                insn(mc, cl)
                try:
                    mc.MOVZX(dstop, cl)
                except FailedToImplement:
                    mc.MOVZX(ecx, cl)
                    mc.MOV(dstop, ecx)

############################################################################
#
# Copyright (C) 2016 The Qt Company Ltd.
# Contact: https://www.qt.io/licensing/
#
# This file is part of Qt Creator.
#
# Commercial License Usage
# Licensees holding valid commercial Qt licenses may use this file in
# accordance with the commercial license agreement provided with the
# Software or, alternatively, in accordance with the terms contained in
# a written agreement between you and The Qt Company. For licensing terms
# and conditions see https://www.qt.io/terms-conditions. For further
# information use the contact form at https://www.qt.io/contact-us.
#
# GNU General Public License Usage
# Alternatively, this file may be used under the terms of the GNU
# General Public License version 3 as published by the Free Software
# Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
# included in the packaging of this file. Please review the following
# information to ensure the GNU General Public License requirements will
# be met: https://www.gnu.org/licenses/gpl-3.0.html.
#
############################################################################

import os
import copy
import struct
import sys
import base64
import re
import time
import json
import inspect

if sys.version_info[0] >= 3:
    xrange = range
    toInteger = int
else:
    toInteger = long


# Debugger start modes. Keep in sync with DebuggerStartMode in debuggerconstants.h
NoStartMode, \
StartInternal, \
StartExternal,  \
AttachExternal,  \
AttachCrashedExternal,  \
AttachCore, \
AttachToRemoteServer, \
AttachToRemoteProcess, \
StartRemoteProcess, \
    = range(0, 9)


# Known special formats. Keep in sync with DisplayFormat in debuggerprotocol.h
AutomaticFormat, \
RawFormat, \
SimpleFormat, \
EnhancedFormat, \
SeparateFormat, \
Latin1StringFormat, \
SeparateLatin1StringFormat, \
Utf8StringFormat, \
SeparateUtf8StringFormat, \
Local8BitStringFormat, \
Utf16StringFormat, \
Ucs4StringFormat, \
Array10Format, \
Array100Format, \
Array1000Format, \
Array10000Format, \
ArrayPlotFormat, \
CompactMapFormat, \
DirectQListStorageFormat, \
IndirectQListStorageFormat, \
    = range(0, 20)

# Breakpoints. Keep synchronized with BreakpointType in breakpoint.h
UnknownType, \
BreakpointByFileAndLine, \
BreakpointByFunction, \
BreakpointByAddress, \
BreakpointAtThrow, \
BreakpointAtCatch, \
BreakpointAtMain, \
BreakpointAtFork, \
BreakpointAtExec, \
BreakpointAtSysCall, \
WatchpointAtAddress, \
WatchpointAtExpression, \
BreakpointOnQmlSignalEmit, \
BreakpointAtJavaScriptThrow, \
    = range(0, 14)


# Internal codes for types
TypeCodeTypedef, \
TypeCodeStruct, \
TypeCodeVoid, \
TypeCodeIntegral, \
TypeCodeFloat, \
TypeCodeEnum, \
TypeCodePointer, \
TypeCodeArray, \
TypeCodeComplex, \
TypeCodeReference, \
TypeCodeFunction, \
TypeCodeMemberPointer, \
TypeCodeFortranString, \
    = range(0, 13)

def isIntegralTypeName(name):
    return name in ('int', 'unsigned int', 'signed int',
                    'short', 'unsigned short',
                    'long', 'unsigned long',
                    'long long', 'unsigned long long',
                    'char', 'signed char', 'unsigned char',
                    'bool')

def isFloatingPointTypeName(name):
    return name in ('float', 'double')


def arrayForms():
    return [ArrayPlotFormat]

def mapForms():
    return [CompactMapFormat]


class ReportItem:
    """
    Helper structure to keep temporary "best" information about a value
    or a type scheduled to be reported. This might get overridden be
    subsequent better guesses during a putItem() run.
    """
    def __init__(self, value = None, encoding = None, priority = -100, elided = None):
        self.value = value
        self.priority = priority
        self.encoding = encoding
        self.elided = elided

    def __str__(self):
        return "Item(value: %s, encoding: %s, priority: %s, elided: %s)" \
            % (self.value, self.encoding, self.priority, self.elided)


def warn(message):
    print('bridgemessage={msg="%s"},' % message.replace('"', '$').encode("latin1"))

def error(message):
    raise RuntimeError(message)


def showException(msg, exType, exValue, exTraceback):
    warn("**** CAUGHT EXCEPTION: %s ****" % msg)
    try:
        import traceback
        for line in traceback.format_exception(exType, exValue, exTraceback):
            warn("%s" % line)
    except:
        pass


class Children:
    def __init__(self, d, numChild = 1, childType = None, childNumChild = None,
            maxNumChild = None, addrBase = None, addrStep = None):
        self.d = d
        self.numChild = numChild
        self.childNumChild = childNumChild
        self.maxNumChild = maxNumChild
        if childType is None:
            self.childType = None
        else:
            self.childType = d.stripClassTag(childType.name)
            if not self.d.isCli:
                self.d.put('childtype="%s",' % self.childType)
            if childNumChild is not None:
                self.d.put('childnumchild="%s",' % childNumChild)
                self.childNumChild = childNumChild
        if addrBase is not None and addrStep is not None:
            self.d.put('addrbase="0x%x",addrstep="%d",' % (addrBase, addrStep))

    def __enter__(self):
        self.savedChildType = self.d.currentChildType
        self.savedChildNumChild = self.d.currentChildNumChild
        self.savedNumChild = self.d.currentNumChild
        self.savedMaxNumChild = self.d.currentMaxNumChild
        self.d.currentChildType = self.childType
        self.d.currentChildNumChild = self.childNumChild
        self.d.currentNumChild = self.numChild
        self.d.currentMaxNumChild = self.maxNumChild
        self.d.put(self.d.childrenPrefix)

    def __exit__(self, exType, exValue, exTraceBack):
        if exType is not None:
            if self.d.passExceptions:
                showException("CHILDREN", exType, exValue, exTraceBack)
            self.d.putSpecialValue("notaccessible")
            self.d.putNumChild(0)
        if self.d.currentMaxNumChild is not None:
            if self.d.currentMaxNumChild < self.d.currentNumChild:
                self.d.put('{name="<incomplete>",value="",type="",numchild="0"},')
        self.d.currentChildType = self.savedChildType
        self.d.currentChildNumChild = self.savedChildNumChild
        self.d.currentNumChild = self.savedNumChild
        self.d.currentMaxNumChild = self.savedMaxNumChild
        if self.d.isCli:
            self.output += '\n' + '   ' * self.indent
        self.d.put(self.d.childrenSuffix)
        return True

class PairedChildrenData:
    def __init__(self, d, pairType, keyType, valueType, useKeyAndValue):
        self.useKeyAndValue = useKeyAndValue
        self.pairType = pairType
        self.keyType = keyType
        self.valueType = valueType
        self.isCompact = d.isMapCompact(self.keyType, self.valueType)
        self.childType = valueType if self.isCompact else pairType

class PairedChildren(Children):
    def __init__(self, d, numChild, useKeyAndValue = False,
            pairType = None, keyType = None, valueType = None, maxNumChild = None):
        self.d = d
        if keyType is None:
            keyType = pairType[0].unqualified()
        if valueType is None:
            valueType = pairType[1]
        d.pairData = PairedChildrenData(d, pairType, keyType, valueType, useKeyAndValue)

        Children.__init__(self, d, numChild,
            d.pairData.childType,
            maxNumChild = maxNumChild,
            addrBase = None, addrStep = None)

    def __enter__(self):
        self.savedPairData = self.d.pairData if hasattr(self.d, "pairData") else None
        Children.__enter__(self)

    def __exit__(self, exType, exValue, exTraceBack):
        Children.__exit__(self, exType, exValue, exTraceBack)
        self.d.pairData = self.savedPairData if self.savedPairData else None


class SubItem:
    def __init__(self, d, component):
        self.d = d
        self.name = component
        self.iname = None

    def __enter__(self):
        self.d.enterSubItem(self)

    def __exit__(self, exType, exValue, exTraceBack):
        return self.d.exitSubItem(self, exType, exValue, exTraceBack)

class TopLevelItem(SubItem):
    def __init__(self, d, iname):
        self.d = d
        self.iname = iname
        self.name = None

class UnnamedSubItem(SubItem):
    def __init__(self, d, component):
        self.d = d
        self.iname = "%s.%s" % (self.d.currentIName, component)
        self.name = None

class DumperBase:
    def __init__(self):
        self.isCdb = False
        self.isGdb = False
        self.isLldb = False
        self.isCli = False

        # Later set, or not set:
        self.stringCutOff = 10000
        self.displayStringLimit = 100

        self.typesReported = {}
        self.typesToReport = {}
        self.qtNamespaceToReport = None

        self.resetCaches()
        self.resetStats()

        self.childrenPrefix = 'children=['
        self.childrenSuffix = '],'

        self.dumpermodules = [
            "qttypes",
            "stdtypes",
            "misctypes",
            "boosttypes",
            "opencvtypes",
            "creatortypes",
            "personaltypes",
        ]


    def resetCaches(self):
        # This is a cache mapping from 'type name' to 'display alternatives'.
        self.qqFormats = { "QVariant (QVariantMap)" : mapForms() }

        # This is a cache of all known dumpers.
        self.qqDumpers = {}    # Direct type match
        self.qqDumpersEx = {}  # Using regexp

        # This is a cache of all dumpers that support writing.
        self.qqEditable = {}

        # This keeps canonical forms of the typenames, without array indices etc.
        self.cachedFormats = {}

        # Maps type names to static metaobjects. If a type is known
        # to not be QObject derived, it contains a 0 value.
        self.knownStaticMetaObjects = {}

        self.counts = {}
        self.structPatternCache = {}
        self.pretimings = {}
        self.timings = []

    def resetStats(self):
        # Timing collection
        self.pretimings = {}
        self.timings = []
        pass

    def dumpStats(self):
        msg = [self.counts, self.timings]
        self.resetStats()
        return msg

    def bump(self, key):
        if key in self.counts:
            self.counts[key] += 1
        else:
            self.counts[key] = 1

    def preping(self, key):
        import time
        self.pretimings[key] = time.time()

    def ping(self, key):
        import time
        elapsed = int(1000000 * (time.time() - self.pretimings[key]))
        self.timings.append([key, elapsed])

    def enterSubItem(self, item):
        if not item.iname:
            item.iname = "%s.%s" % (self.currentIName, item.name)
        if not self.isCli:
            self.put('{')
            if isinstance(item.name, str):
                self.put('name="%s",' % item.name)
        else:
            self.indent += 1
            self.output += '\n' + '   ' * self.indent
            if isinstance(item.name, str):
                self.output += item.name + ' = '
        item.savedIName = self.currentIName
        item.savedValue = self.currentValue
        item.savedType = self.currentType
        self.currentIName = item.iname
        self.currentValue = ReportItem();
        self.currentType = ReportItem();


    def exitSubItem(self, item, exType, exValue, exTraceBack):
        #warn("CURRENT VALUE: %s: %s %s" %
        # (self.currentIName, self.currentValue, self.currentType))
        if not exType is None:
            if self.passExceptions:
                showException("SUBITEM", exType, exValue, exTraceBack)
            self.putSpecialValue("notaccessible")
            self.putNumChild(0)
        if not self.isCli:
            try:
                if self.currentType.value:
                    typeName = self.currentType.value
                    if len(typeName) > 0 and typeName != self.currentChildType:
                        self.put('type="%s",' % typeName) # str(type.GetUnqualifiedType()) ?
                if self.currentValue.value is None:
                    self.put('value="",encoding="notaccessible",numchild="0",')
                else:
                    if not self.currentValue.encoding is None:
                        self.put('valueencoded="%s",' % self.currentValue.encoding)
                    if self.currentValue.elided:
                        self.put('valueelided="%s",' % self.currentValue.elided)
                    self.put('value="%s",' % self.currentValue.value)
            except:
                pass
            self.put('},')
        else:
            self.indent -= 1
            try:
                if self.currentType.value:
                    typeName = self.stripClassTag(self.currentType.value)
                    self.put('<%s> = {' % typeName)

                if  self.currentValue.value is None:
                    self.put('<not accessible>')
                else:
                    value = self.currentValue.value
                    if self.currentValue.encoding == "latin1":
                        value = self.hexdecode(value)
                    elif self.currentValue.encoding == "utf8":
                        value = self.hexdecode(value)
                    elif self.currentValue.encoding == "utf16":
                        b = bytes.fromhex(value)
                        value = codecs.decode(b, 'utf-16')
                    self.put('"%s"' % value)
                    if self.currentValue.elided:
                        self.put('...')

                if self.currentType.value:
                    self.put('}')
            except:
                pass
        self.currentIName = item.savedIName
        self.currentValue = item.savedValue
        self.currentType = item.savedType
        return True

    def stripClassTag(self, typeName):
        if not isinstance(typeName, str):
            error("Expected string in stripClassTag(), got %s" % type(typeName))
        if typeName.startswith("class "):
            return typeName[6:]
        if typeName.startswith("struct "):
            return typeName[7:]
        if typeName.startswith("const "):
            return typeName[6:]
        if typeName.startswith("volatile "):
            return typeName[9:]
        return typeName

    def stripForFormat(self, typeName):
        if not isinstance(typeName, str):
            error("Expected string in stripForFormat(), got %s" % type(typeName))
        if typeName in self.cachedFormats:
            return self.cachedFormats[typeName]
        stripped = ""
        inArray = 0
        for c in self.stripClassTag(typeName):
            if c == '<':
                break
            if c == ' ':
                continue
            if c == '[':
                inArray += 1
            elif c == ']':
                inArray -= 1
            if inArray and ord(c) >= 48 and ord(c) <= 57:
                continue
            stripped +=  c
        self.cachedFormats[typeName] = stripped
        return stripped

    # Hex decoding operating on str, return str.
    def hexdecode(self, s):
        if sys.version_info[0] == 2:
            return s.decode("hex")
        return bytes.fromhex(s).decode("utf8")

    # Hex encoding operating on str or bytes, return str.
    def hexencode(self, s):
        if s is None:
            s = ''
        if sys.version_info[0] == 2:
            if isinstance(s, buffer):
                return bytes(s).encode("hex")
            return s.encode("hex")
        if isinstance(s, str):
            s = s.encode("utf8")
        return base64.b16encode(s).decode("utf8")

    def isQt3Support(self):
        # assume no Qt 3 support by default
        return False

    # Clamps size to limit.
    def computeLimit(self, size, limit):
        if limit == 0:
            limit = self.displayStringLimit
        if limit is None or size <= limit:
            return 0, size
        return size, limit

    def vectorDataHelper(self, addr):
        if self.qtVersion() >= 0x050000:
            if self.ptrSize() == 4:
                (ref, size, alloc, offset) = self.split("IIIp", addr)
            else:
                (ref, size, alloc, pad, offset) = self.split("IIIIp", addr)
            alloc = alloc & 0x7ffffff
            data = addr + offset
        else:
            (ref, alloc, size) = self.split("III", addr)
            data = addr + 16
        self.check(0 <= size and size <= alloc and alloc <= 1000 * 1000 * 1000)
        return data, size, alloc

    def byteArrayDataHelper(self, addr):
        if self.qtVersion() >= 0x050000:
            # QTypedArray:
            # - QtPrivate::RefCount ref
            # - int size
            # - uint alloc : 31, capacityReserved : 1
            # - qptrdiff offset
            (ref, size, alloc, offset) = self.split("IIpp", addr)
            alloc = alloc & 0x7ffffff
            data = addr + offset
            if self.ptrSize() == 4:
                data = data & 0xffffffff
            else:
                data = data & 0xffffffffffffffff
        elif self.qtVersion() >= 0x040000:
            # Data:
            # - QBasicAtomicInt ref;
            # - int alloc, size;
            # - [padding]
            # - char *data;
            if self.ptrSize() == 4:
                (ref, alloc, size, data) = self.split("IIIp", addr)
            else:
                (ref, alloc, size, pad, data) = self.split("IIIIp", addr)
        else:
            # Data:
            # - QShared count;
            # - QChar *unicode
            # - char *ascii
            # - uint len: 30
            (dummy, dummy, dummy, size) = self.split("IIIp", addr)
            size = self.extractInt(addr + 3 * self.ptrSize()) & 0x3ffffff
            alloc = size  # pretend.
            data = self.extractPointer(addr + self.ptrSize())
        return data, size, alloc

    # addr is the begin of a QByteArrayData structure
    def encodeStringHelper(self, addr, limit):
        # Should not happen, but we get it with LLDB as result
        # of inferior calls
        if addr == 0:
            return 0, ""
        data, size, alloc = self.byteArrayDataHelper(addr)
        if alloc != 0:
            self.check(0 <= size and size <= alloc and alloc <= 100*1000*1000)
        elided, shown = self.computeLimit(size, limit)
        return elided, self.readMemory(data, 2 * shown)

    def encodeByteArrayHelper(self, addr, limit):
        data, size, alloc = self.byteArrayDataHelper(addr)
        if alloc != 0:
            self.check(0 <= size and size <= alloc and alloc <= 100*1000*1000)
        elided, shown = self.computeLimit(size, limit)
        return elided, self.readMemory(data, shown)

    def putCharArrayHelper(self, data, size, charType,
                           displayFormat = AutomaticFormat,
                           makeExpandable = True):
        charSize = charType.size()
        bytelen = size * charSize
        elided, shown = self.computeLimit(bytelen, self.displayStringLimit)
        mem = self.readMemory(data, shown)
        if charSize == 1:
            if displayFormat in (Latin1StringFormat, SeparateLatin1StringFormat):
                encodingType = "latin1"
            else:
                encodingType = "utf8"
            #childType = "char"
        elif charSize == 2:
            encodingType = "utf16"
            #childType = "short"
        else:
            encodingType = "ucs4"
            #childType = "int"

        self.putValue(mem, encodingType, elided=elided)

        if displayFormat in (SeparateLatin1StringFormat, SeparateUtf8StringFormat, SeparateFormat):
            elided, shown = self.computeLimit(bytelen, 100000)
            self.putDisplay(encodingType + ':separate', self.readMemory(data, shown))

        if makeExpandable:
            self.putNumChild(size)
            if self.isExpanded():
                with Children(self):
                    for i in range(size):
                        self.putSubItem(size, self.createValue(data + i * charSize, charType))

    def readMemory(self, addr, size):
        return self.hexencode(bytes(self.readRawMemory(addr, size)))

    def encodeByteArray(self, value, limit = 0):
        elided, data = self.encodeByteArrayHelper(self.extractPointer(value), limit)
        return data

    def byteArrayData(self, value):
        return self.byteArrayDataHelper(self.extractPointer(value))

    def putByteArrayValue(self, value):
        elided, data = self.encodeByteArrayHelper(self.extractPointer(value), self.displayStringLimit)
        self.putValue(data, "latin1", elided=elided)

    def encodeString(self, value, limit = 0):
        elided, data = self.encodeStringHelper(self.extractPointer(value), limit)
        return data

    def encodedUtf16ToUtf8(self, s):
        return ''.join([chr(int(s[i:i+2], 16)) for i in range(0, len(s), 4)])

    def encodeStringUtf8(self, value, limit = 0):
        return self.encodedUtf16ToUtf8(self.encodeString(value, limit))

    def stringData(self, value):
        return self.byteArrayDataHelper(self.extractPointer(value))

    def encodeStdString(self, value, limit = 0):
        data = value["_M_dataplus"]["_M_p"]
        sizePtr = data.cast(self.sizetType().pointer())
        size = int(sizePtr[-3])
        alloc = int(sizePtr[-2])
        self.check(0 <= size and size <= alloc and alloc <= 100*1000*1000)
        elided, shown = self.computeLimit(size, limit)
        return self.readMemory(data, shown)

    def extractTemplateArgument(self, typename, position):
        level = 0
        skipSpace = False
        inner = ''
        for c in typename[typename.find('<') + 1 : -1]:
            if c == '<':
                inner += c
                level += 1
            elif c == '>':
                level -= 1
                inner += c
            elif c == ',':
                if level == 0:
                    if position == 0:
                        return inner.strip()
                    position -= 1
                    inner = ''
                else:
                    inner += c
                    skipSpace = True
            else:
                if skipSpace and c == ' ':
                    pass
                else:
                    inner += c
                    skipSpace = False
        # Handle local struct definitions like QList<main(int, char**)::SomeStruct>
        inner = inner.strip()
        p = inner.find(')::')
        if p > -1:
            inner = inner[p+3:]
        return inner

    def putStringValueByAddress(self, addr):
        elided, data = self.encodeStringHelper(addr, self.displayStringLimit)
        self.putValue(data, "utf16", elided=elided)

    def putStringValue(self, value):
        elided, data = self.encodeStringHelper(self.extractPointer(value), self.displayStringLimit)
        self.putValue(data, "utf16", elided=elided)

    def putIntItem(self, name, value):
        with SubItem(self, name):
            self.putValue(value)
            self.putType("int")
            self.putNumChild(0)

    def putBoolItem(self, name, value):
        with SubItem(self, name):
            self.putValue(value)
            self.putType("bool")
            self.putNumChild(0)

    def putPairItem(self, index, pair):
        if isinstance(pair, tuple):
            (first, second) = pair
        elif self.pairData.useKeyAndValue:
            (first, second) = (pair["key"], pair["value"])
        else:
            (first, second) = (pair["first"], pair["second"])

        with SubItem(self, index):
            self.putNumChild(2)
            (keystr, keyenc, valstr, valenc) = (None, None, None, None)
            with Children(self):
                with SubItem(self, "key"):
                    self.putItem(first, True)
                    keystr = self.currentValue.value
                    keyenc = self.currentValue.encoding
                with SubItem(self, "value"):
                    self.putItem(second, True)
                    valstr = self.currentValue.value
                    valenc = self.currentValue.encoding
            if index is not None:
                self.put('keyprefix="[%s] ",' % index)
            self.put('keyencoded="%s",key="%s",' % (keyenc, keystr))
            self.putValue(valstr, valenc)

    def putCallItem(self, name, rettype, value, func, *args):
        with SubItem(self, name):
            try:
                result = self.callHelper(rettype, value, func, args)
            except Exception as error:
                if self.passExceptions:
                    raise error
                else:
                    children = [('error', error)]
                    self.putSpecialValue("notcallable", children=children)
            else:
                self.putItem(result)

    def call(self, rettype, value, func, *args):
        return self.callHelper(rettype, value, func, args)

    def putAddress(self, address):
        if address is not None and not self.isCli:
            self.put('address="0x%x",' % address)

    def putPlainChildren(self, value, dumpBase = True):
        self.putEmptyValue(-99)
        self.putNumChild(1)
        if self.isExpanded():
            with Children(self):
                self.putFields(value, dumpBase)

    def putNamedChildren(self, values, names):
        self.putEmptyValue(-99)
        self.putNumChild(1)
        if self.isExpanded():
            with Children(self):
                for n, v in zip(names, values):
                    self.putSubItem(n, v)

    def putFields(self, value, dumpBase = True):
        for field in value.type.fields():
            #warn("FIELD: %s" % field)
            if field.name is not None and field.name.startswith("_vptr."):
                with SubItem(self, "[vptr]"):
                    # int (**)(void)
                    n = 100
                    self.putType(" ")
                    self.put('sortgroup="20"')
                    self.putValue(field.name)
                    self.putNumChild(n)
                    if self.isExpanded():
                        with Children(self):
                            p = value[field.name]
                            for i in xrange(n):
                                if p.dereference().integer() != 0:
                                    with SubItem(self, i):
                                        self.putItem(p.dereference())
                                        self.putType(" ")
                                        p = p + 1
                continue

            if field.isBaseClass and dumpBase:
                # We cannot use nativeField.name as part of the iname as
                # it might contain spaces and other strange characters.
                with UnnamedSubItem(self, "@%d" % (field.baseIndex + 1)):
                    baseValue = value[field]
                    self.put('iname="%s",' % self.currentIName)
                    self.put('name="[%s]",' % field.name)
                    self.put('sortgroup="%s"' % (1000 - field.baseIndex))
                    self.putAddress(baseValue.address())
                    self.putItem(baseValue, False)
                continue

            with SubItem(self, field.name):
                self.putItem(value[field])


    def putMembersItem(self, value, sortorder = 10):
        with SubItem(self, "[members]"):
            self.put('sortgroup="%s"' % sortorder)
            self.putPlainChildren(value)

    def isMapCompact(self, keyType, valueType):
        if self.currentItemFormat() == CompactMapFormat:
            return True
        return keyType.isSimpleType() and valueType.isSimpleType()

    def check(self, exp):
        if not exp:
            error("Check failed: %s" % exp)

    def checkRef(self, ref):
        # Assume there aren't a million references to any object.
        self.check(ref >= -1)
        self.check(ref < 1000000)

    def checkIntType(self, thing):
        if not self.isInt(thing):
            error("Expected an integral value, got %s" % type(thing))

    def readToFirstZero(self, base, tsize, maximum):
        self.checkIntType(base)
        self.checkIntType(tsize)
        self.checkIntType(maximum)

        code = (None, "b", "H", None, "I")[tsize]
        #blob = self.readRawMemory(base, maximum)

        blob = bytes()
        while maximum > 1:
            try:
                blob = self.readRawMemory(base, maximum)
                break
            except:
                maximum = int(maximum / 2)
                warn("REDUCING READING MAXIMUM TO %s" % maximum)

        #warn("BASE: 0x%x TSIZE: %s MAX: %s" % (base, tsize, maximum))
        for i in xrange(0, maximum, tsize):
            t = struct.unpack_from(code, blob, i)[0]
            if t == 0:
                return 0, i, self.hexencode(blob[:i])

        # Real end is unknown.
        return -1, maximum, self.hexencode(blob[:maximum])

    def encodeCArray(self, p, tsize, limit):
        elided, shown, blob = self.readToFirstZero(p, tsize, limit)
        return elided, blob

    def putItemCount(self, count, maximum = 1000000000):
        # This needs to override the default value, so don't use 'put' directly.
        if count > maximum:
            self.putSpecialValue("minimumitemcount", maximum)
        else:
            self.putSpecialValue("itemcount", count)
        self.putNumChild(count)

    def resultToMi(self, value):
        if type(value) is bool:
            return '"%d"' % int(value)
        if type(value) is dict:
            return '{' + ','.join(['%s=%s' % (k, self.resultToMi(v))
                                for (k, v) in list(value.items())]) + '}'
        if type(value) is list:
            return '[' + ','.join([self.resultToMi(k)
                                for k in list(value.items())]) + ']'
        return '"%s"' % value

    def variablesToMi(self, value, prefix):
        if type(value) is bool:
            return '"%d"' % int(value)
        if type(value) is dict:
            pairs = []
            for (k, v) in list(value.items()):
                if k == 'iname':
                    if v.startswith('.'):
                        v = '"%s%s"' % (prefix, v)
                    else:
                        v = '"%s"' % v
                else:
                    v = self.variablesToMi(v, prefix)
                pairs.append('%s=%s' % (k, v))
            return '{' + ','.join(pairs) + '}'
        if type(value) is list:
            index = 0
            pairs = []
            for item in value:
                if item.get('type', '') == 'function':
                    continue
                name = item.get('name', '')
                if len(name) == 0:
                    name = str(index)
                    index += 1
                pairs.append((name, self.variablesToMi(item, prefix)))
            pairs.sort(key = lambda pair: pair[0])
            return '[' + ','.join([pair[1] for pair in pairs]) + ']'
        return '"%s"' % value

    def filterPrefix(self, prefix, items):
        return [i[len(prefix):] for i in items if i.startswith(prefix)]

    def tryFetchInterpreterVariables(self, args):
        if not int(args.get('nativemixed', 0)):
            return (False, '')
        context = args.get('context', '')
        if not len(context):
            return (False, '')

        expanded = args.get('expanded')
        args['expanded'] = self.filterPrefix('local', expanded)

        res = self.sendInterpreterRequest('variables', args)
        if not res:
            return (False, '')

        reslist = []
        for item in res.get('variables', {}):
            if not 'iname' in item:
                item['iname'] = '.' + item.get('name')
            reslist.append(self.variablesToMi(item, 'local'))

        watchers = args.get('watchers', None)
        if watchers:
            toevaluate = []
            name2expr = {}
            seq = 0
            for watcher in watchers:
                expr = self.hexdecode(watcher.get('exp'))
                name = str(seq)
                toevaluate.append({'name': name, 'expression': expr})
                name2expr[name] = expr
                seq += 1
            args['expressions'] = toevaluate

            args['expanded'] = self.filterPrefix('watch', expanded)
            del args['watchers']
            res = self.sendInterpreterRequest('expressions', args)

            if res:
                for item in res.get('expressions', {}):
                    name = item.get('name')
                    iname = 'watch.' + name
                    expr = name2expr.get(name)
                    item['iname'] = iname
                    item['wname'] = self.hexencode(expr)
                    item['exp'] = expr
                    reslist.append(self.variablesToMi(item, 'watch'))

        return (True, 'data=[%s]' % ','.join(reslist))

    def putField(self, name, value):
        self.put('%s="%s",' % (name, value))

    def putType(self, typish, priority = 0):
        # Higher priority values override lower ones.
        if priority >= self.currentType.priority:
            if isinstance(typish, str):
                self.currentType.value = typish
            else:
                self.currentType.value = typish.name
            self.currentType.priority = priority

    def putValue(self, value, encoding = None, priority = 0, elided = None):
        # Higher priority values override lower ones.
        # elided = 0 indicates all data is available in value,
        # otherwise it's the true length.
        if priority >= self.currentValue.priority:
            self.currentValue = ReportItem(value, encoding, priority, elided)

    def putSpecialValue(self, encoding, value = "", children = None):
        self.putValue(value, encoding)
        if children is not None:
            self.putNumChild(1)
            if self.isExpanded():
                with Children(self):
                    for name, value in children:
                        with SubItem(self, name):
                            self.putValue(str(value).replace('"', '$'))

    def putEmptyValue(self, priority = -10):
        if priority >= self.currentValue.priority:
            self.currentValue = ReportItem("", None, priority, None)

    def putName(self, name):
        self.put('name="%s",' % name)

    def putBetterType(self, typish):
        if isinstance(typish, ReportItem):
            self.currentType.value = typish.value
        elif isinstance(typish, str):
            self.currentType.value = typish
        else:
            self.currentType.value = typish.name
        self.currentType.priority += 1

    def putNoType(self):
        # FIXME: replace with something that does not need special handling
        # in SubItem.__exit__().
        self.putBetterType(" ")

    def putInaccessible(self):
        #self.putBetterType(" ")
        self.putNumChild(0)
        self.currentValue.value = None

    def putNamedSubItem(self, component, value, name):
        with SubItem(self, component):
            self.putName(name)
            self.putItem(value)

    def isExpanded(self):
        #warn("IS EXPANDED: %s in %s: %s" % (self.currentIName,
        #    self.expandedINames, self.currentIName in self.expandedINames))
        return self.currentIName in self.expandedINames

    def mangleName(self, typeName):
        return '_ZN%sE' % ''.join(map(lambda x: "%d%s" % (len(x), x),
            typeName.split('::')))

    def putCStyleArray(self, value):
        arrayType = value.type.unqualified()
        if self.isGdb and value.nativeValue is not None:
            innerType = self.fromNativeType(value.nativeValue[0].type)
        else:
            innerType = value.type.target()
        innerTypeName = innerType.unqualified().name
        address = value.address()
        if address:
            self.putValue("@0x%x" % address, priority = -1)
        else:
            self.putEmptyValue()
        self.putType(arrayType)

        displayFormat = self.currentItemFormat()
        arrayByteSize = arrayType.size()
        if arrayByteSize == 0:
            # This should not happen. But it does, see QTCREATORBUG-14755.
            # GDB/GCC produce sizeof == 0 for QProcess arr[3]
            s = str(value.type)
            itemCount = s[s.find('[')+1:s.find(']')]
            if not itemCount:
                itemCount = '100'
            arrayByteSize = int(itemCount) * innerType.size();

        n = int(arrayByteSize / innerType.size())
        p = value.address()
        if displayFormat != RawFormat and p:
            if innerTypeName in ("char", "wchar_t"):
                self.putCharArrayHelper(p, n, innerType, self.currentItemFormat(),
                                        makeExpandable = False)
            else:
                self.tryPutSimpleFormattedPointer(p, arrayType, innerTypeName,
                    displayFormat, arrayByteSize)
        self.putNumChild(n)

        if self.isExpanded():
            self.putArrayData(p, n, innerType)

        self.putPlotDataHelper(p, n, innerType)

    def cleanAddress(self, addr):
        if addr is None:
            return "<no address>"
        return "0x%x" % toInteger(hex(addr), 16)

    def stripNamespaceFromType(self, typeName):
        typename = self.stripClassTag(typeName)
        ns = self.qtNamespace()
        if len(ns) > 0 and typename.startswith(ns):
            typename = typename[len(ns):]
        pos = typename.find("<")
        # FIXME: make it recognize  foo<A>::bar<B>::iterator?
        while pos != -1:
            pos1 = typename.rfind(">", pos)
            typename = typename[0:pos] + typename[pos1+1:]
            pos = typename.find("<")
        return typename

    def tryPutPrettyItem(self, typeName, value):
        value.check()
        if self.useFancy and self.currentItemFormat() != RawFormat:
            self.putType(typeName)

            nsStrippedType = self.stripNamespaceFromType(typeName)\
                .replace("::", "__")

            #warn("STRIPPED: %s" % nsStrippedType)
            # The following block is only needed for D.
            if nsStrippedType.startswith("_A"):
                # DMD v2.058 encodes string[] as _Array_uns long long.
                # With spaces.
                if nsStrippedType.startswith("_Array_"):
                    qdump_Array(self, value)
                    return True
                if nsStrippedType.startswith("_AArray_"):
                    qdump_AArray(self, value)
                    return True

            dumper = self.qqDumpers.get(nsStrippedType)
            #warn("DUMPER: %s" % dumper)
            if dumper is not None:
                dumper(self, value)
                return True

            for pattern in self.qqDumpersEx.keys():
                dumper = self.qqDumpersEx[pattern]
                if re.match(pattern, nsStrippedType):
                    dumper(self, value)
                    return True

        return False

    def putSimpleCharArray(self, base, size = None):
        if size is None:
            elided, shown, data = self.readToFirstZero(base, 1, self.displayStringLimit)
        else:
            elided, shown = self.computeLimit(int(size), self.displayStringLimit)
            data = self.readMemory(base, shown)
        self.putValue(data, "latin1", elided=elided)

    def putDisplay(self, editFormat, value):
        self.put('editformat="%s",' % editFormat)
        self.put('editvalue="%s",' % value)

    # This is shared by pointer and array formatting.
    def tryPutSimpleFormattedPointer(self, ptr, typeName, innerTypeName, displayFormat, limit):
        if displayFormat == AutomaticFormat:
            if innerTypeName == "char":
                # Use UTF-8 as default for char *.
                self.putType(typeName)
                (elided, data) = self.encodeCArray(ptr, 1, limit)
                self.putValue(data, "utf8", elided=elided)
                return True

            if innerTypeName == "wchar_t":
                self.putType(typeName)
                charSize = self.lookupType('wchar_t').size()
                (elided, data) = self.encodeCArray(ptr, charSize, limit)
                if charSize == 2:
                    self.putValue(data, "utf16", elided=elided)
                else:
                    self.putValue(data, "ucs4", elided=elided)
                return True

        if displayFormat == Latin1StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 1, limit)
            self.putValue(data, "latin1", elided=elided)
            return True

        if displayFormat == SeparateLatin1StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 1, limit)
            self.putValue(data, "latin1", elided=elided)
            self.putDisplay("latin1:separate", data)
            return True

        if displayFormat == Utf8StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 1, limit)
            self.putValue(data, "utf8", elided=elided)
            return True

        if displayFormat == SeparateUtf8StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 1, limit)
            self.putValue(data, "utf8", elided=elided)
            self.putDisplay("utf8:separate", data)
            return True

        if displayFormat == Local8BitStringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 1, limit)
            self.putValue(data, "local8bit", elided=elided)
            return True

        if displayFormat == Utf16StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 2, limit)
            self.putValue(data, "utf16", elided=elided)
            return True

        if displayFormat == Ucs4StringFormat:
            self.putType(typeName)
            (elided, data) = self.encodeCArray(ptr, 4, limit)
            self.putValue(data, "ucs4", elided=elided)
            return True

        return False

    def putFormattedPointer(self, value):
        pointer = value.pointer()
        #warn("POINTER: %s" % pointer)
        if pointer == 0:
            #warn("NULL POINTER")
            self.putType(value.type)
            self.putValue("0x0")
            self.putNumChild(0)
            return

        typeName = value.type.name

        self.putAddress(pointer)
        self.putOriginalAddress(value)

        try:
            self.readRawMemory(pointer, 1)
        except:
            # Failure to dereference a pointer should at least
            # show the value of a pointer.
            #warn("BAD POINTER: %s" % value)
            self.putValue("0x%x" % pointer)
            self.putType(typeName)
            self.putNumChild(0)
            return

        displayFormat = self.currentItemFormat(value.type.name)
        innerType = value.type.target().unqualified()
        innerTypeName = innerType.name

        if innerTypeName == "void":
            #warn("VOID POINTER: %s" % displayFormat)
            self.putType(typeName)
            self.putValue("0x%x" % pointer)
            self.putNumChild(0)
            return

        if displayFormat == RawFormat:
            # Explicitly requested bald pointer.
            #warn("RAW")
            self.putType(typeName)
            self.putValue(self.hexencode(str(value)), "utf8:1:0")
            self.putNumChild(1)
            if self.currentIName in self.expandedINames:
                with Children(self):
                    with SubItem(self, '*'):
                        self.putItem(value.dereference())
            return

        limit = self.displayStringLimit
        if displayFormat in (SeparateLatin1StringFormat, SeparateUtf8StringFormat):
            limit = 1000000
        if self.tryPutSimpleFormattedPointer(pointer, typeName,
                                             innerTypeName, displayFormat, limit):
            self.putNumChild(0)
            return

        if Array10Format <= displayFormat and displayFormat <= Array1000Format:
            n = (10, 100, 1000, 10000)[displayFormat - Array10Format]
            self.putType(typeName)
            self.putItemCount(n)
            self.putArrayData(value.address(), n, innerType)
            return

        if innerType.code == TypeCodeFunction:
            # A function pointer.
            self.putValue("0x%x" % pointer)
            self.putType(typeName)
            self.putNumChild(0)
            return

        #warn("AUTODEREF: %s" % self.autoDerefPointers)
        #warn("INAME: %s" % self.currentIName)
        #warn("INNER: %s" % innerTypeName)
        if self.autoDerefPointers or self.currentIName.endswith('.this'):
            # Generic pointer type with AutomaticFormat.
            # Never dereference char types.
            if innerTypeName not in ("char", "signed char", "unsigned char", "wchar_t"):
                self.putType(innerTypeName)
                savedCurrentChildType = self.currentChildType
                self.currentChildType = self.stripClassTag(innerTypeName)
                self.putItem(value.dereference())
                self.currentChildType = savedCurrentChildType
                self.putOriginalAddress(value)
                return

        #warn("GENERIC PLAIN POINTER: %s" % value.type)
        #warn("ADDR PLAIN POINTER: 0x%x" % value.address)
        self.putType(typeName)
        self.putValue("0x%x" % pointer)
        self.putNumChild(1)
        if self.currentIName in self.expandedINames:
            with Children(self):
                with SubItem(self, "*"):
                    self.putItem(value.dereference())

    def putOriginalAddress(self, value):
        if value.address() is not None:
            self.put('origaddr="0x%x",' % value.address())

    def putQObjectNameValue(self, value):
        try:
            intSize = 4
            ptrSize = self.ptrSize()
            # dd = value["d_ptr"]["d"] is just behind the vtable.
            (vtable, dd) = self.split("pp", value)

            if self.qtVersion() < 0x050000:
                # Size of QObjectData: 5 pointer + 2 int
                #  - vtable
                #   - QObject *q_ptr;
                #   - QObject *parent;
                #   - QObjectList children;
                #   - uint isWidget : 1; etc..
                #   - int postedEvents;
                #   - QMetaObject *metaObject;

                # Offset of objectName in QObjectPrivate: 5 pointer + 2 int
                #   - [QObjectData base]
                #   - QString objectName
                objectName = self.extractPointer(dd + 5 * ptrSize + 2 * intSize)

            else:
                # Size of QObjectData: 5 pointer + 2 int
                #   - vtable
                #   - QObject *q_ptr;
                #   - QObject *parent;
                #   - QObjectList children;
                #   - uint isWidget : 1; etc...
                #   - int postedEvents;
                #   - QDynamicMetaObjectData *metaObject;
                extra = self.extractPointer(dd + 5 * ptrSize + 2 * intSize)
                if extra == 0:
                    return False

                # Offset of objectName in ExtraData: 6 pointer
                #   - QVector<QObjectUserData *> userData; only #ifndef QT_NO_USERDATA
                #   - QList<QByteArray> propertyNames;
                #   - QList<QVariant> propertyValues;
                #   - QVector<int> runningTimers;
                #   - QList<QPointer<QObject> > eventFilters;
                #   - QString objectName
                objectName = self.extractPointer(extra + 5 * ptrSize)

            data, size, alloc = self.byteArrayDataHelper(objectName)

            # Object names are short, and GDB can crash on to big chunks.
            # Since this here is a convenience feature only, limit it.
            if size <= 0 or size > 80:
                return False

            raw = self.readMemory(data, 2 * size)
            self.putValue(raw, "utf16", 1)
            return True

        except:
        #    warn("NO QOBJECT: %s" % value.type)
            pass

    def couldBeQObject(self, objectPtr):
        def canBePointer(p):
            if self.ptrSize() == 4:
                return p > 100000 and (p & 0x3 == 0)
            else:
                return p > 100000 and (p & 0x7 == 0) and (p < 0x7fffffffffff)

        try:
            (vtablePtr, dd) = self.split('pp', objectPtr)
        except:
            self.bump("nostruct-1")
            return False
        if not canBePointer(vtablePtr):
            self.bump("vtable")
            return False
        if not canBePointer(dd):
            self.bump("d_d_ptr")
            return False

        try:
            (dvtablePtr, qptr, parentPtr, childrenDPtr, flags) \
                = self.split('ppppI', dd)
        except:
            self.bump("nostruct-2")
            return False
        #warn("STRUCT DD: %s 0x%x" % (self.currentIName, qptr))
        if not canBePointer(dvtablePtr):
            self.bump("dvtable")
            #warn("DVT: 0x%x" % dvtablePtr)
            return False
        # Check d_ptr.d.q_ptr == objectPtr
        if qptr != objectPtr:
            #warn("QPTR: 0x%x 0x%x" % (qptr, objectPtr))
            self.bump("q_ptr")
            return False
        if parentPtr and not canBePointer(parentPtr):
            #warn("PAREN")
            self.bump("parent")
            return False
        if not canBePointer(childrenDPtr):
            #warn("CHILD")
            self.bump("children")
            return False
        #if flags >= 0x80: # Only 7 flags are defined
        #    warn("FLAGS: 0x%x %s" % (flags, self.currentIName))
        #    self.bump("flags")
        #    return False
        #warn("OK")
        #if dynMetaObjectPtr and not canBePointer(dynMetaObjectPtr):
        #    self.bump("dynmo")
        #    return False

        self.bump("couldBeQObject")
        return True


    def extractMetaObjectPtr(self, objectPtr, typeobj):
        """ objectPtr - address of *potential* instance of QObject derived class
            typeobj - type of *objectPtr if known, None otherwise. """

        if objectPtr is not None:
            self.checkIntType(objectPtr)

        def extractMetaObjectPtrFromAddress():
            return 0
            # FIXME: Calling "works" but seems to impact memory contents(!)
            # in relevant places. One symptom is that object name
            # contents "vanishes" as the reported size of the string
            # gets zeroed out(?).
            # Try vtable, metaObject() is the first entry.
            vtablePtr = self.extractPointer(objectPtr)
            metaObjectFunc = self.extractPointer(vtablePtr)
            cmd = "((void*(*)(void*))0x%x)((void*)0x%x)" % (metaObjectFunc, objectPtr)
            try:
                #warn("MO CMD: %s" % cmd)
                res = self.parseAndEvaluate(cmd)
                #warn("MO RES: %s" % res)
                self.bump("successfulMetaObjectCall")
                return toInteger(res)
            except:
                self.bump("failedMetaObjectCall")
                #warn("COULD NOT EXECUTE: %s" % cmd)
            return 0

        def extractStaticMetaObjectFromTypeHelper(someTypeObj):
            if someTypeObj.isSimpleType():
                return 0

            typeName = someTypeObj.name
            isQObjectProper = typeName == self.qtNamespace() + "QObject"

            if not isQObjectProper:
                if someTypeObj.firstBase() is None:
                    return 0

                # No templates for now.
                if typeName.find('<') >= 0:
                    return 0

            result = self.findStaticMetaObject(typeName)

            # We need to distinguish Q_OBJECT from Q_GADGET:
            # a Q_OBJECT SMO has a non-null superdata (unless it's QObject itself),
            # a Q_GADGET SMO has a null superdata (hopefully)
            if result and not isQObjectProper:
                superdata = self.extractPointer(result)
                if superdata == 0:
                    # This looks like a Q_GADGET
                    return 0

            return result

        def extractStaticMetaObjectPtrFromType(someTypeObj):
            if someTypeObj is None:
                return 0
            someTypeName = someTypeObj.name
            self.bump('metaObjectFromType')
            known = self.knownStaticMetaObjects.get(someTypeName, None)
            if known is not None: # Is 0 or the static metaobject.
                return known

            result = 0
            #try:
            result = extractStaticMetaObjectFromTypeHelper(someTypeObj)
            #except RuntimeError as error:
            #    warn("METAOBJECT EXTRACTION FAILED: %s" % error)
            #except:
            #    warn("METAOBJECT EXTRACTION FAILED FOR UNKNOWN REASON")

            if not result:
                base = someTypeObj.firstBase()
                if base is not None and base != someTypeObj: # sanity check
                    result = extractStaticMetaObjectPtrFromType(base)

            self.knownStaticMetaObjects[someTypeName] = result
            return result


        if not self.useFancy:
            return 0

        ptrSize = self.ptrSize()

        typeName = typeobj.name
        result = self.knownStaticMetaObjects.get(typeName, None)
        if result is not None: # Is 0 or the static metaobject.
            self.bump("typecached")
            #warn("CACHED RESULT: %s %s 0x%x" % (self.currentIName, typeName, result))
            return result

        if not self.couldBeQObject(objectPtr):
            self.bump('cannotBeQObject')
            #warn("DOES NOT LOOK LIKE A QOBJECT: %s" % self.currentIName)
            return 0

        metaObjectPtr = 0
        if not metaObjectPtr:
            # measured: 3 ms (example had one level of inheritance)
            self.preping("metaObjectType-" + self.currentIName)
            metaObjectPtr = extractStaticMetaObjectPtrFromType(typeobj)
            self.ping("metaObjectType-" + self.currentIName)

        if not metaObjectPtr:
            # measured: 200 ms (example had one level of inheritance)
            self.preping("metaObjectCall-" + self.currentIName)
            metaObjectPtr = extractMetaObjectPtrFromAddress()
            self.ping("metaObjectCall-" + self.currentIName)

        #if metaObjectPtr:
        #    self.bump('foundMetaObject')
        #    self.knownStaticMetaObjects[typeName] = metaObjectPtr

        return metaObjectPtr

    def split(self, pattern, value):
        if isinstance(value, self.Value):
            return value.split(pattern)
        if self.isInt(value):
            val = self.Value(self)
            val.laddress = value
            return val.split(pattern)
        error("CANNOT EXTRACT STRUCT FROM %s" % type(value))

    def extractCString(self, addr):
        result = bytearray()
        while True:
            d = self.extractByte(addr)
            if d == 0:
                break
            result.append(d)
            addr += 1
        return result

    def listChildrenGenerator(self, addr, innerType):
        base = self.extractPointer(addr)
        (ref, alloc, begin, end) = self.split('IIII', base)
        array = base + 16
        if self.qtVersion() < 0x50000:
            array += self.ptrSize()
        size = end - begin
        stepSize = self.ptrSize()
        data = array + begin * stepSize
        for i in range(size):
            yield self.createValue(data + i * stepSize, innerType)
            #yield self.createValue(data + i * stepSize, "void*")

    def vectorChildrenGenerator(self, addr, innerType):
        base = self.extractPointer(addr)
        size = self.extractInt(base + 4)
        data = base + self.extractPointer(base + 8 + self.ptrSize())
        for i in range(size):
            yield self.createValue(data + i * 16, innerType)

    def putTypedPointer(self, name, addr, typeName):
        """ Prints a typed pointer, expandable if the type can be resolved,
            and without children otherwise """
        with SubItem(self, name):
            self.putAddress(addr)
            self.putValue("@0x%x" % addr)
            typeObj = self.lookupType(typeName)
            if typeObj:
                self.putType(typeObj)
                self.putNumChild(1)
                if self.isExpanded():
                    with Children(self):
                        self.putFields(self.createValue(addr, typeObj))
            else:
                self.putType(typeName)
                self.putNumChild(0)

    # This is called is when a QObject derived class is expanded
    def putQObjectGuts(self, qobject, metaObjectPtr):
        self.putQObjectGutsHelper(qobject, qobject.address(), -1, metaObjectPtr, "QObject")

    def metaString(self, metaObjectPtr, index, revision):
        ptrSize = self.ptrSize()
        stringdata = self.extractPointer(toInteger(metaObjectPtr) + ptrSize)
        if revision >= 7: # Qt 5.
            byteArrayDataSize = 24 if ptrSize == 8 else 16
            literal = stringdata + toInteger(index) * byteArrayDataSize
            ldata, lsize, lalloc = self.byteArrayDataHelper(literal)
            try:
                s = struct.unpack_from("%ds" % lsize, self.readRawMemory(ldata, lsize))[0]
                return s if sys.version_info[0] == 2 else s.decode("utf8")
            except:
                return "<not available>"
        else: # Qt 4.
            ldata = stringdata + index
            return self.extractCString(ldata).decode("utf8")

    def putQMetaStuff(self, value, origType):
        (metaObjectPtr, handle) = value.split('pI')
        if metaObjectPtr != 0:
            dataPtr = self.extractPointer(metaObjectPtr + 2 * self.ptrSize())
            index = self.extractInt(dataPtr + 4 * handle)
            revision = 7 if self.qtVersion() >= 0x050000 else 6
            name = self.metaString(metaObjectPtr, index, revision)
            self.putValue(name)
            self.putNumChild(1)
            if self.isExpanded():
                with Children(self):
                    self.putFields(value)
                    self.putQObjectGutsHelper(0, 0, handle, metaObjectPtr, origType)
        else:
            self.putEmptyValue()
            if self.isExpanded():
                with Children(self):
                    self.putFields(value)

    # basically all meta things go through this here.
    # qobject and qobjectPtr are non-null  if coming from a real structure display
    # qobject == 0, qobjectPtr != 0 is possible for builds without QObject debug info
    #   if qobject == 0, properties and d-ptr cannot be shown.
    # handle is what's store in QMetaMethod etc, pass -1 for QObject/QMetaObject
    # itself metaObjectPtr needs to point to a valid QMetaObject.
    def putQObjectGutsHelper(self, qobject, qobjectPtr, handle, metaObjectPtr, origType):
        intSize = 4
        ptrSize = self.ptrSize()

        def putt(name, value, typeName = ' '):
            with SubItem(self, name):
                self.putValue(value)
                self.putType(typeName)
                self.putNumChild(0)

        def extractSuperDataPtr(someMetaObjectPtr):
            #return someMetaObjectPtr['d']['superdata']
            return self.extractPointer(someMetaObjectPtr)

        def extractDataPtr(someMetaObjectPtr):
            # dataPtr = metaObjectPtr["d"]["data"]
            return self.extractPointer(someMetaObjectPtr + 2 * ptrSize)

        isQMetaObject = origType == "QMetaObject"
        isQObject = origType == "QObject"

        #warn("OBJECT GUTS: %s 0x%x " % (self.currentIName, metaObjectPtr))
        dataPtr = extractDataPtr(metaObjectPtr)
        #warn("DATA PTRS: %s 0x%x " % (self.currentIName, dataPtr))
        (revision, classname,
            classinfo, classinfo2,
            methodCount, methods,
            propertyCount, properties,
            enumCount, enums,
            constructorCount, constructors,
            flags, signalCount) = self.split('I' * 14, dataPtr)

        largestStringIndex = -1
        for i in range(methodCount):
            t = self.split('IIIII', dataPtr + 56 + i * 20)
            if largestStringIndex < t[0]:
                largestStringIndex = t[0]

        ns = self.qtNamespace()
        extraData = 0
        if qobjectPtr:
            dd = self.extractPointer(qobjectPtr + ptrSize)
            if self.qtVersion() >= 0x50000:
                (dvtablePtr, qptr, parentPtr, childrenDPtr, flags, postedEvents,
                    dynMetaObjectPtr, # Up to here QObjectData.
                    extraData, threadDataPtr, connectionListsPtr,
                    sendersPtr, currentSenderPtr) \
                        = self.split('ppppIIp' + 'ppppp', dd)
            else:
                (dvtablePtr, qptr, parentPtr, childrenDPtr, flags, postedEvents,
                    dynMetaObjectPtr, # Up to here QObjectData
                    objectName, extraData, threadDataPtr, connectionListsPtr,
                    sendersPtr, currentSenderPtr) \
                        = self.split('ppppIIp' + 'pppppp', dd)

        if qobjectPtr:
            qobjectType = self.createType(ns + "QObject")
            qobjectPtrType = self.createType(ns + "QObject") # FIXME.
            with SubItem(self, "[parent]"):
                self.put('sortgroup="9"')
                self.putItem(self.createValue(dd + 2 * ptrSize, qobjectPtrType))
            with SubItem(self, "[children]"):
                self.put('sortgroup="8"')
                base = self.extractPointer(dd + 3 * ptrSize) # It's a QList<QObject *>
                begin = self.extractInt(base + 8)
                end = self.extractInt(base + 12)
                array = base + 16
                if self.qtVersion() < 0x50000:
                    array += ptrSize
                self.check(begin >= 0 and end >= 0 and end <= 1000 * 1000 * 1000)
                size = end - begin
                self.check(size >= 0)
                self.putItemCount(size)
                if self.isExpanded():
                    addrBase = array + begin * ptrSize
                    with Children(self, size):
                        for i in self.childRange():
                            with SubItem(self, i):
                                childPtr = self.extractPointer(addrBase + i * ptrSize)
                                self.putItem(self.createValue(childPtr, qobjectType))

        if isQMetaObject:
            with SubItem(self, "[strings]"):
                self.put('sortgroup="2"')
                if largestStringIndex > 0:
                    self.putSpecialValue("minimumitemcount", largestStringIndex)
                    self.putNumChild(1)
                    if self.isExpanded():
                        with Children(self, largestStringIndex + 1):
                            for i in self.childRange():
                                with SubItem(self, i):
                                    s = self.metaString(metaObjectPtr, i, revision)
                                    self.putValue(self.hexencode(s), "latin1")
                                    self.putNumChild(0)
                else:
                    self.putValue(" ")
                    self.putNumChild(0)

        if isQMetaObject:
            with SubItem(self, "[raw]"):
                self.put('sortgroup="1"')
                self.putEmptyValue()
                self.putNumChild(1)
                if self.isExpanded():
                    with Children(self):
                        putt("revision", revision)
                        putt("classname", classname)
                        putt("classinfo", classinfo)
                        putt("methods", "%d %d" % (methodCount, methods))
                        putt("properties", "%d %d" % (propertyCount, properties))
                        putt("enums/sets", "%d %d" % (enumCount, enums))
                        putt("constructors", "%d %d" % (constructorCount, constructors))
                        putt("flags", flags)
                        putt("signalCount", signalCount)
                        for i in range(methodCount):
                            t = self.split('IIIII', dataPtr + 56 + i * 20)
                            putt("method %d" % i, "%s %s %s %s %s" % t)

        if isQObject:
            with SubItem(self, "[extra]"):
                self.put('sortgroup="1"')
                self.putEmptyValue()
                self.putNumChild(1)
                if self.isExpanded():
                    with Children(self):
                        if extraData:
                            self.putTypedPointer("[extraData]", extraData,
                                 ns + "QObjectPrivate::ExtraData")

                        if connectionListsPtr:
                            self.putTypedPointer("[connectionLists]", connectionListsPtr,
                                 ns + "QObjectConnectionListVector")

                        with SubItem(self, "[metaObject]"):
                            self.putAddress(metaObjectPtr)
                            self.putNumChild(1)
                            if self.isExpanded():
                                with Children(self):
                                    self.putQObjectGutsHelper(0, 0, -1, metaObjectPtr, "QMetaObject")


        if isQMetaObject or isQObject:
            with SubItem(self, "[properties]"):
                self.put('sortgroup="5"')
                if self.isExpanded():
                    dynamicPropertyCount = 0
                    with Children(self):
                        # Static properties.
                        for i in range(propertyCount):
                            t = self.split("III", dataPtr + properties * 4 + 12 * i)
                            name = self.metaString(metaObjectPtr, t[0], revision)
                            if qobject:
                                # LLDB doesn't like calling it on a derived class, possibly
                                # due to type information living in a different shared object.
                                base = self.createValue(qobjectPtr, ns + "QObject")
                                self.putCallItem(name, ns + "QVariant", base, "property", '"' + name + '"')
                            else:
                                putt(name, ' ')

                        # Dynamic properties.
                        if extraData:
                            byteArrayType = self.createType("QByteArray")
                            variantType = self.createType("QVariant")
                            if self.qtVersion() >= 0x50700:
                                values = self.vectorChildrenGenerator(
                                    extraData + 2 * ptrSize, variantType)
                            elif self.qtVersion() >= 0x50000:
                                values = self.listChildrenGenerator(
                                    extraData + 2 * ptrSize, variantType)
                            else:
                                values = self.listChildrenGenerator(
                                    extraData + 2 * ptrSize, variantType.pointer())
                            names = self.listChildrenGenerator(
                                    extraData + ptrSize, byteArrayType)
                            for (k, v) in zip(names, values):
                                with SubItem(self, propertyCount + dynamicPropertyCount):
                                    self.put('key="%s",' % self.encodeByteArray(k))
                                    self.put('keyencoded="latin1",')
                                    self.putItem(v)
                                    dynamicPropertyCount += 1
                    self.putItemCount(propertyCount + dynamicPropertyCount)
                else:
                    # We need a handle to [x] for the user to expand the item
                    # before we know whether there are actual children. Counting
                    # them is too expensive.
                    self.putSpecialValue("minimumitemcount", propertyCount)
                    self.putNumChild(1)

        superDataPtr = extractSuperDataPtr(metaObjectPtr)

        globalOffset = 0
        superDataIterator = superDataPtr
        while superDataIterator:
            sdata = extractDataPtr(superDataIterator)
            globalOffset += self.extractInt(sdata + 16) # methodCount member
            superDataIterator = extractSuperDataPtr(superDataIterator)

        if isQMetaObject or isQObject:
            with SubItem(self, "[methods]"):
                self.put('sortgroup="3"')
                self.putItemCount(methodCount)
                if self.isExpanded():
                    with Children(self):
                        for i in range(methodCount):
                            t = self.split("IIIII", dataPtr + 56 + 20 * i)
                            name = self.metaString(metaObjectPtr, t[0], revision)
                            with SubItem(self, i):
                                self.putValue(name)
                                self.putType(" ")
                                self.putNumChild(1)
                                isSignal = False
                                flags = t[4]
                                if flags == 0x06:
                                    typ = "signal"
                                    isSignal = True
                                elif flags == 0x0a:
                                    typ = "slot"
                                elif flags == 0x0a:
                                    typ = "invokable"
                                else:
                                    typ = "<unknown>"
                                with Children(self):
                                    putt("[nameindex]", t[0])
                                    putt("[type]", typ)
                                    putt("[argc]", t[1])
                                    putt("[parameter]", t[2])
                                    putt("[tag]", t[3])
                                    putt("[flags]", t[4])
                                    putt("[localindex]", str(i))
                                    putt("[globalindex]", str(globalOffset + i))

        if isQObject:
            with SubItem(self, "[d]"):
                self.putItem(self.createValue(dd, self.qtNamespace() + "QObjectPrivate"))
                self.put('sortgroup="15"')

        if isQMetaObject:
            with SubItem(self, "[superdata]"):
                self.put('sortgroup="12"')
                if superDataPtr:
                    self.putType(self.qtNamespace() + "QMetaObject")
                    self.putAddress(superDataPtr)
                    self.putNumChild(1)
                    if self.isExpanded():
                        with Children(self):
                            self.putQObjectGutsHelper(0, 0, -1, superDataPtr, "QMetaObject")
                else:
                    self.putType(self.qtNamespace() + "QMetaObject *")
                    self.putValue("0x0")
                    self.putNumChild(0)

        if handle >= 0:
            localIndex = int((handle - methods) / 5)
            with SubItem(self, "[localindex]"):
                self.put('sortgroup="12"')
                self.putValue(localIndex)
            with SubItem(self, "[globalindex]"):
                self.put('sortgroup="11"')
                self.putValue(globalOffset + localIndex)


        #with SubItem(self, "[signals]"):
        #    self.putItemCount(signalCount)
        #    signalNames = metaData(52, -14, 5)
        #    warn("NAMES: %s" % signalNames)
        #    if self.isExpanded():
        #        with Children(self):
        #            putt("A", "b")
        #            for i in range(signalCount):
        #                k = signalNames[i]
        #                with SubItem(self, k):
        #                    self.putEmptyValue()
        #            if dd:
        #                self.putQObjectConnections(dd)

    def putQObjectConnections(self, dd):
        with SubItem(self, "[connections]"):
            ptrSize = self.ptrSize()
            self.putNoType()
            ns = self.qtNamespace()
            privateTypeName = ns + "QObjectPrivate"
            privateType = self.lookupType(privateTypeName)
            d_ptr = dd.cast(privateType.pointer()).dereference()
            connections = d_ptr["connectionLists"]
            if self.connections.integer() == 0:
                self.putItemCount(0)
            else:
                connections = connections.dereference()
                connections = connections.cast(connections.type.firstBase())
                self.putSpecialValue("minimumitemcount", 0)
                self.putNumChild(1)
            if self.isExpanded():
                pp = 0
                with Children(self):
                    innerType = connections.type[0]
                    # Should check:  innerType == ns::QObjectPrivate::ConnectionList
                    base = self.extractPointer(connections)
                    data, size, alloc = self.vectorDataHelper(base)
                    connectionType = self.lookupType(ns + "QObjectPrivate::Connection*")
                    for i in xrange(size):
                        first = self.extractPointer(data + i * 2 * ptrSize)
                        while first:
                            self.putSubItem("%s" % pp,
                                self.createValue(first, connectionType))
                            first = self.extractPointer(first + 3 * ptrSize)
                            # We need to enforce some upper limit.
                            pp += 1
                            if pp > 1000:
                                break

    def currentItemFormat(self, typeName = None):
        displayFormat = self.formats.get(self.currentIName, AutomaticFormat)
        if displayFormat == AutomaticFormat:
            if typeName is None:
                typeName = self.currentType.value
            needle = None if typeName is None else self.stripForFormat(typeName)
            displayFormat = self.typeformats.get(needle, AutomaticFormat)
        return displayFormat

    def putSubItem(self, component, value, tryDynamic=True):
        if not isinstance(value, self.Value):
            error("WRONG VALUE TYPE IN putSubItem: %s" % type(value))
        if not isinstance(value.type, self.Type):
            error("WRONG TYPE TYPE IN putSubItem: %s" % type(value.type))
        with SubItem(self, component):
            self.putItem(value, tryDynamic)

    def putArrayData(self, base, n, innerType, childNumChild = None, maxNumChild = 10000):
        self.checkIntType(base)
        self.checkIntType(n)
        addrBase = base
        innerSize = innerType.size()
        #warn("ADDRESS: %s INNERSIZE: %s INNERTYPE: %s" % (addrBase, innerSize, innerType))
        enc = innerType.simpleEncoding()
        if enc:
            self.put('childtype="%s",' % innerType.name)
            self.put('addrbase="0x%x",' % addrBase)
            self.put('addrstep="0x%x",' % innerSize)
            self.put('arrayencoding="%s",' % enc)
            if n > maxNumChild:
                self.put('childrenelided="%s",' % n) # FIXME: Act on that in frontend
                n = maxNumChild
            self.put('arraydata="')
            self.put(self.readMemory(addrBase, n * innerSize))
            self.put('",')
        else:
            with Children(self, n, innerType, childNumChild, maxNumChild,
                    addrBase=addrBase, addrStep=innerSize):
                for i in self.childRange():
                    self.putSubItem(i, self.createValue(addrBase + i * innerSize, innerType))

    def putArrayItem(self, name, addr, n, typeName):
        self.checkIntType(addr)
        self.checkIntType(n)
        with SubItem(self, name):
            self.putEmptyValue()
            self.putType("%s [%d]" % (typeName, n))
            self.putArrayData(addr, n, self.lookupType(typeName))
            self.putAddress(addr)

    def putPlotDataHelper(self, base, n, innerType, maxNumChild = 1000*1000):
        if n > maxNumChild:
            self.put('plotelided="%s",' % n) # FIXME: Act on that in frontend
            n = maxNumChild
        if self.currentItemFormat() == ArrayPlotFormat and innerType.isSimpleType():
            enc = innerType.simpleEncoding()
            if enc:
                self.putField("editencoding", enc)
                self.putDisplay("plotdata:separate",
                                self.readMemory(base, n * innerType.size()))

    def putPlotData(self, base, n, innerType, maxNumChild = 1000*1000):
        self.putPlotDataHelper(base, n, innerType, maxNumChild=maxNumChild)
        if self.isExpanded():
            self.putArrayData(base, n, innerType, maxNumChild=maxNumChild)

    def putSpecialArgv(self, value):
        """
        Special handling for char** argv.
        """
        n = 0
        p = value
        # p is 0 for "optimized out" cases. Or contains rubbish.
        try:
            if value.integer():
                while p.dereference().integer() and n <= 100:
                    p += 1
                    n += 1
        except:
            pass

        with TopLevelItem(self, 'local.argv'):
            self.put('iname="local.argv",name="argv",')
            self.putItemCount(n, 100)
            self.putType('char **')
            if self.currentIName in self.expandedINames:
                p = value
                with Children(self, n):
                    for i in xrange(n):
                        self.putSubItem(i, p.dereference())
                        p += 1

    def extractPointer(self, value):
        code = "I" if self.ptrSize() == 4 else "Q"
        return self.extractSomething(value, code, 8 * self.ptrSize())

    def extractInt64(self, value):
        return self.extractSomething(value, "q", 64)

    def extractUInt64(self, value):
        return self.extractSomething(value, "Q", 64)

    def extractInt(self, value):
        return self.extractSomething(value, "i", 32)

    def extractUInt(self, value):
        return self.extractSomething(value, "I", 32)

    def extractShort(self, value):
        return self.extractSomething(value, "h", 16)

    def extractUShort(self, value):
        return self.extractSomething(value, "H", 16)

    def extractByte(self, value):
        return self.extractSomething(value, "b", 8)

    def extractSomething(self, value, pattern, bitsize):
        if self.isInt(value):
            val = self.Value(self)
            val.laddress = value
            return val.extractSomething(pattern, bitsize)
        if isinstance(value, self.Value):
            return value.extractSomething(pattern, bitsize)
        error("CANT EXTRACT FROM %s" % type(value))

    # Parses a..b and  a.(s).b
    def parseRange(self, exp):

        # Search for the first unbalanced delimiter in s
        def searchUnbalanced(s, upwards):
            paran = 0
            bracket = 0
            if upwards:
                open_p, close_p, open_b, close_b = '(', ')', '[', ']'
            else:
                open_p, close_p, open_b, close_b = ')', '(', ']', '['
            for i in range(len(s)):
                c = s[i]
                if c == open_p:
                    paran += 1
                elif c == open_b:
                    bracket += 1
                elif c == close_p:
                    paran -= 1
                    if paran < 0:
                        return i
                elif c == close_b:
                    bracket -= 1
                    if bracket < 0:
                        return i
            return len(s)

        match = re.search("(\.)(\(.+?\))?(\.)", exp)
        if match:
            s = match.group(2)
            left_e = match.start(1)
            left_s =  1 + left_e - searchUnbalanced(exp[left_e::-1], False)
            right_s = match.end(3)
            right_e = right_s + searchUnbalanced(exp[right_s:], True)
            template = exp[:left_s] + '%s' +  exp[right_e:]

            a = exp[left_s:left_e]
            b = exp[right_s:right_e]

            try:
                # Allow integral expressions.
                ss = self.parseAndEvaluate(s[1:len(s)-1]).integer() if s else 1
                aa = self.parseAndEvaluate(a).integer()
                bb = self.parseAndEvaluate(b).integer()
                if aa < bb and ss > 0:
                    return True, aa, ss, bb + 1, template
            except:
                pass
        return False, 0, 1, 1, exp

    def putNumChild(self, numchild):
        if numchild != self.currentChildNumChild:
            self.put('numchild="%s",' % numchild)

    def handleLocals(self, variables):
        #warn("VARIABLES: %s" % variables)
        self.preping("locals")
        shadowed = {}
        for value in variables:
            self.anonNumber = 0
            if value.name == "argv" and value.type.name == "char **":
                self.putSpecialArgv(value)
            else:
                name = value.name
                if name in shadowed:
                    level = shadowed[name]
                    shadowed[name] = level + 1
                    name += "@%d" % level
                else:
                    shadowed[name] = 1
                # A "normal" local variable or parameter.
                iname = value.iname if  hasattr(value, 'iname') else 'local.' + name
                with TopLevelItem(self, iname):
                    self.preping("all-" + iname)
                    self.put('iname="%s",name="%s",' % (iname, name))
                    self.putItem(value)
                    self.ping("all-" + iname)
        self.ping("locals")

    def handleWatches(self, args):
        self.preping("watches")
        for watcher in args.get("watchers", []):
            iname = watcher['iname']
            exp = self.hexdecode(watcher['exp'])
            self.handleWatch(exp, exp, iname)
        self.ping("watches")

    def handleWatch(self, origexp, exp, iname):
        exp = str(exp).strip()
        escapedExp = self.hexencode(exp)
        #warn("HANDLING WATCH %s -> %s, INAME: '%s'" % (origexp, exp, iname))

        # Grouped items separated by semicolon
        if exp.find(";") >= 0:
            exps = exp.split(';')
            n = len(exps)
            with TopLevelItem(self, iname):
                self.put('iname="%s",' % iname)
                #self.put('wname="%s",' % escapedExp)
                self.put('name="%s",' % exp)
                self.put('exp="%s",' % exp)
                self.putItemCount(n)
                self.putNoType()
            for i in xrange(n):
                self.handleWatch(exps[i], exps[i], "%s.%d" % (iname, i))
            return

        # Special array index: e.g a[1..199] or a[1.(3).199] for stride 3.
        isRange, begin, step, end, template = self.parseRange(exp)
        if isRange:
            #warn("RANGE: %s %s %s in %s" % (begin, step, end, template))
            r = range(begin, end, step)
            n = len(r)
            with TopLevelItem(self, iname):
                self.put('iname="%s",' % iname)
                #self.put('wname="%s",' % escapedExp)
                self.put('name="%s",' % exp)
                self.put('exp="%s",' % exp)
                self.putItemCount(n)
                self.putNoType()
                with Children(self, n):
                    for i in r:
                        e = template % i
                        self.handleWatch(e, e, "%s.%s" % (iname, i))
            return

            # Fall back to less special syntax
            #return self.handleWatch(origexp, exp, iname)

        with TopLevelItem(self, iname):
            self.put('iname="%s",' % iname)
            self.put('wname="%s",' % escapedExp)
            try:
                value = self.parseAndEvaluate(exp)
                self.putItem(value)
            except Exception:
                self.currentType.value = " "
                self.currentValue.value = "<no such value>"
                self.currentChildNumChild = -1
                self.currentNumChild = 0
                self.putNumChild(0)

    def registerDumper(self, funcname, function):
        try:
            if funcname.startswith("qdump__"):
                typename = funcname[7:]
                spec = inspect.getargspec(function)
                if len(spec.args) == 2:
                    self.qqDumpers[typename] = function
                elif len(spec.args) == 3 and len(spec.defaults) == 1:
                    self.qqDumpersEx[spec.defaults[0]] = function
                self.qqFormats[typename] = self.qqFormats.get(typename, [])
            elif funcname.startswith("qform__"):
                typename = funcname[7:]
                try:
                    self.qqFormats[typename] = function()
                except:
                    self.qqFormats[typename] = []
            elif funcname.startswith("qedit__"):
                typename = funcname[7:]
                try:
                    self.qqEditable[typename] = function
                except:
                    pass
        except:
            pass

    def setupDumpers(self, _ = {}):
        self.resetCaches()

        for mod in self.dumpermodules:
            m = __import__(mod)
            dic = m.__dict__
            for name in dic.keys():
                item = dic[name]
                self.registerDumper(name, item)

        msg = "dumpers=["
        for key, value in self.qqFormats.items():
            editable = ',editable="true"' if key in self.qqEditable else ''
            formats = (',formats=\"%s\"' % str(value)[1:-1]) if len(value) else ''
            msg += '{type="%s"%s%s},' % (key, editable, formats)
        msg += '],'
        v = 10000 * sys.version_info[0] + 100 * sys.version_info[1] + sys.version_info[2]
        msg += 'python="%d"' % v
        return msg

    def reloadDumpers(self, args):
        for mod in self.dumpermodules:
            m = sys.modules[mod]
            if sys.version_info[0] >= 3:
                import importlib
                importlib.reload(m)
            else:
                reload(m)
        self.setupDumpers(args)

    def addDumperModule(self, args):
        path = args['path']
        (head, tail) = os.path.split(path)
        sys.path.insert(1, head)
        self.dumpermodules.append(os.path.splitext(tail)[0])

    def extractQStringFromQDataStream(self, buf, offset):
        """ Read a QString from the stream """
        size = struct.unpack_from("!I", buf, offset)[0]
        offset += 4
        string = buf[offset:offset + size].decode('utf-16be')
        return (string, offset + size)

    def extractQByteArrayFromQDataStream(self, buf, offset):
        """ Read a QByteArray from the stream """
        size = struct.unpack_from("!I", buf, offset)[0]
        offset += 4
        string = buf[offset:offset + size].decode('latin1')
        return (string, offset + size)

    def extractIntFromQDataStream(self, buf, offset):
        """ Read an int from the stream """
        value = struct.unpack_from("!I", buf, offset)[0]
        return (value, offset + 4)

    def handleInterpreterMessage(self):
        """ Return True if inferior stopped """
        resdict = self.fetchInterpreterResult()
        return resdict.get('event') == 'break'

    def reportInterpreterResult(self, resdict, args):
        print('interpreterresult=%s,token="%s"'
            % (self.resultToMi(resdict), args.get('token', -1)))

    def reportInterpreterAsync(self, resdict, asyncclass):
        print('interpreterasync=%s,asyncclass="%s"'
            % (self.resultToMi(resdict), asyncclass))

    def removeInterpreterBreakpoint(self, args):
        res = self.sendInterpreterRequest('removebreakpoint', { 'id' : args['id'] })
        return res

    def insertInterpreterBreakpoint(self, args):
        args['condition'] = self.hexdecode(args.get('condition', ''))
        # Will fail if the service is not yet up and running.
        response = self.sendInterpreterRequest('setbreakpoint', args)
        resdict = args.copy()
        bp = None if response is None else response.get("breakpoint", None)
        if bp:
            resdict['number'] = bp
            resdict['pending'] = 0
        else:
            self.createResolvePendingBreakpointsHookBreakpoint(args)
            resdict['number'] = -1
            resdict['pending'] = 1
            resdict['warning'] = 'Direct interpreter breakpoint insertion failed.'
        self.reportInterpreterResult(resdict, args)

    def resolvePendingInterpreterBreakpoint(self, args):
        self.parseAndEvaluate('qt_qmlDebugEnableService("NativeQmlDebugger")')
        response = self.sendInterpreterRequest('setbreakpoint', args)
        bp = None if response is None else response.get("breakpoint", None)
        resdict = args.copy()
        if bp:
            resdict['number'] = bp
            resdict['pending'] = 0
        else:
            resdict['number'] = -1
            resdict['pending'] = 0
            resdict['error'] = 'Pending interpreter breakpoint insertion failed.'
        self.reportInterpreterAsync(resdict, 'breakpointmodified')

    def fetchInterpreterResult(self):
        buf = self.parseAndEvaluate("qt_qmlDebugMessageBuffer")
        size = self.parseAndEvaluate("qt_qmlDebugMessageLength")
        msg = self.hexdecode(self.readMemory(buf, size))
        # msg is a sequence of 'servicename<space>msglen<space>msg' items.
        resdict = {}  # Native payload.
        while len(msg):
            pos0 = msg.index(' ') # End of service name
            pos1 = msg.index(' ', pos0 + 1) # End of message length
            service = msg[0:pos0]
            msglen = int(msg[pos0+1:pos1])
            msgend = pos1+1+msglen
            payload = msg[pos1+1:msgend]
            msg = msg[msgend:]
            if service == 'NativeQmlDebugger':
                try:
                    resdict = json.loads(payload)
                    continue
                except:
                    warn("Cannot parse native payload: %s" % payload)
            else:
                print('interpreteralien=%s'
                    % {'service': service, 'payload': self.hexencode(payload)})
        try:
            expr = 'qt_qmlDebugClearBuffer()'
            res = self.parseAndEvaluate(expr)
        except RuntimeError as error:
            warn("Cleaning buffer failed: %s: %s" % (expr, error))

        return resdict

    def sendInterpreterRequest(self, command, args = {}):
        encoded = json.dumps({ 'command': command, 'arguments': args })
        hexdata = self.hexencode(encoded)
        expr = 'qt_qmlDebugSendDataToService("NativeQmlDebugger","%s")' % hexdata
        try:
            res = self.parseAndEvaluate(expr)
        except RuntimeError as error:
            warn("Interpreter command failed: %s: %s" % (encoded, error))
            return {}
        except AttributeError as error:
            # Happens with LLDB and 'None' current thread.
            warn("Interpreter command failed: %s: %s" % (encoded, error))
            return {}
        if not res:
            warn("Interpreter command failed: %s " % encoded)
            return {}
        return self.fetchInterpreterResult()

    def executeStep(self, args):
        if self.nativeMixed:
            response = self.sendInterpreterRequest('stepin', args)
        self.doContinue()

    def executeStepOut(self, args):
        if self.nativeMixed:
            response = self.sendInterpreterRequest('stepout', args)
        self.doContinue()

    def executeNext(self, args):
        if self.nativeMixed:
            response = self.sendInterpreterRequest('stepover', args)
        self.doContinue()

    def executeContinue(self, args):
        if self.nativeMixed:
            response = self.sendInterpreterRequest('continue', args)
        self.doContinue()

    def doInsertInterpreterBreakpoint(self, args, wasPending):
        #warn("DO INSERT INTERPRETER BREAKPOINT, WAS PENDING: %s" % wasPending)
        # Will fail if the service is not yet up and running.
        response = self.sendInterpreterRequest('setbreakpoint', args)
        bp = None if response is None else response.get("breakpoint", None)
        if wasPending:
            if not bp:
                self.reportInterpreterResult({'bpnr': -1, 'pending': 1,
                    'error': 'Pending interpreter breakpoint insertion failed.'}, args)
                return
        else:
            if not bp:
                self.reportInterpreterResult({'bpnr': -1, 'pending': 1,
                    'warning': 'Direct interpreter breakpoint insertion failed.'}, args)
                self.createResolvePendingBreakpointsHookBreakpoint(args)
                return
        self.reportInterpreterResult({'bpnr': bp, 'pending': 0}, args)

    def isInternalInterpreterFrame(self, functionName):
        if functionName is None:
            return False
        if functionName.startswith("qt_v4"):
            return True
        return functionName.startswith(self.qtNamespace() + "QV4::")

    # Hack to avoid QDate* dumper timeouts with GDB 7.4 on 32 bit
    # due to misaligned %ebx in SSE calls (qstring.cpp:findChar)
    def canCallLocale(self):
        return True

    def isReportableInterpreterFrame(self, functionName):
        return functionName and functionName.find("QV4::Moth::VME::exec") >= 0

    def extractQmlData(self, value):
        if value.type.code == TypeCodePointer:
            value = value.dereference()
        data = value["data"]
        return data.cast(self.lookupType(value.type.name.replace("QV4::", "QV4::Heap::")))

    def extractInterpreterStack(self):
        return self.sendInterpreterRequest('backtrace', {'limit': 10 })

    def isInt(self, thing):
        if isinstance(thing, int):
            return True
        if sys.version_info[0] == 2:
            if isinstance(thing, long):
                return True
        return False

    def putItem(self, value, tryDynamic=True):
        #warn("ITEM: %s" % value.stringify())

        typeobj = value.type #unqualified()
        typeName = typeobj.name

        tryDynamic &= self.useDynamicType
        self.addToCache(typeobj) # Fill type cache
        if tryDynamic:
            self.putAddress(value.address())

        if not value.isInScope():
            self.putSpecialValue("optimizedout")
            #self.putValue("optimizedout: %s" % value.nativeValue)
            #self.putType(typeobj)
            #self.putSpecialValue('outofscope')
            self.putNumChild(0)
            return

        if not isinstance(value, self.Value):
            error("WRONG TYPE IN putItem: %s" % type(self.Value))

        # Try on possibly typedefed type first.
        if self.tryPutPrettyItem(typeName, value):
            return

        if typeobj.code == TypeCodeTypedef:
            strippedType = typeobj.stripTypedefs()
            self.putItem(value.cast(strippedType))
            self.putBetterType(typeName)
            return

        if typeobj.code == TypeCodePointer:
            self.putFormattedPointer(value)
            return

        if typeobj.code == TypeCodeFunction:
            self.putType(typeobj)
            self.putValue(value)
            self.putNumChild(0)
            return

        if typeobj.code == TypeCodeEnum:
            #warn("ENUM VALUE: %s" % value.stringify())
            self.putType(typeobj.name)
            self.putValue(value.display())
            self.putNumChild(0)
            return

        if typeobj.code == TypeCodeArray:
            #warn("ARRAY VALUE: %s" % value)
            self.putCStyleArray(value)
            return

        if typeobj.code == TypeCodeIntegral:
            #warn("INTEGER: %s %s" % (value.name, value))
            self.putValue(value.value())
            self.putNumChild(0)
            self.putType(typeobj.name)
            return

        if typeobj.code == TypeCodeFloat:
            #warn("FLOAT VALUE: %s" % value)
            self.putValue(value.value())
            self.putNumChild(0)
            self.putType(typeobj.name)
            return

        if typeobj.code == TypeCodeReference:
            try:
                # Try to recognize null references explicitly.
                if value.address() is 0:
                    self.putSpecialValue("nullreference")
                    self.putNumChild(0)
                    self.putType(typeobj)
                    return
            except:
                pass

            if self.isLldb:
                targetType = value.type.target()
                item = value.cast(targetType.pointer()).dereference()
                self.putItem(item)
                self.putBetterType(value.type.name)
                return

            else:
                if tryDynamic:
                    try:
                        # Dynamic references are not supported by gdb, see
                        # http://sourceware.org/bugzilla/show_bug.cgi?id=14077.
                        # Find the dynamic type manually using referenced_type.
                        val = value.referenced_value()
                        val = val.cast(val.dynamic_type)
                        self.putItem(val)
                        self.putBetterType("%s &" % typeobj)
                        return
                    except:
                        pass

                try:
                    # FIXME: This throws "RuntimeError: Attempt to dereference a
                    # generic pointer." with MinGW's gcc 4.5 when it "identifies"
                    # a "QWidget &" as "void &" and with optimized out code.
                    self.putItem(value.cast(typeobj.target().unqualified()))
                    self.putBetterType("%s &" % self.currentType.value)
                    return
                except Exception as error:
                    self.putSpecialValue("optimizedout")
                    #self.putValue("optimizedout: %s" % error)
                    self.putType(typeobj)
                    self.putNumChild(0)
                    return

        if typeobj.code == TypeCodeComplex:
            self.putType(typeobj)
            self.putValue(value.display())
            self.putNumChild(0)
            return

        if typeobj.code == TypeCodeFortranString:
            data = self.value.data()
            self.putValue(data, "latin1", 1)
            self.putType(typeobj)

        if typeName.endswith("[]"):
            # D arrays, gdc compiled.
            n = value["length"]
            base = value["ptr"]
            self.putType(typeName)
            self.putItemCount(n)
            if self.isExpanded():
                self.putArrayData(base.type.target(), base, n)
            return

        #warn("SOME VALUE: %s" % value)
        #warn("HAS CHILDREN VALUE: %s" % value.hasChildren())
        #warn("GENERIC STRUCT: %s" % typeobj)
        #warn("INAME: %s " % self.currentIName)
        #warn("INAMES: %s " % self.expandedINames)
        #warn("EXPANDED: %s " % (self.currentIName in self.expandedINames))
        self.putType(typeName)
        self.putNumChild(1)
        self.putEmptyValue()
        #warn("STRUCT GUTS: %s  ADDRESS: %s " % (value.name, value.address()))
        #metaObjectPtr = self.extractMetaObjectPtr(value.address(), value.type)
        if self.showQObjectNames:
            self.preping(self.currentIName)
            metaObjectPtr = self.extractMetaObjectPtr(value.address(), value.type)
            self.ping(self.currentIName)
            if metaObjectPtr:
                self.context = value
            self.putQObjectNameValue(value)
        #warn("STRUCT GUTS: %s  MO: 0x%x " % (self.currentIName, metaObjectPtr))
        if self.isExpanded():
            self.put('sortable="1"')
            with Children(self, 1, childType=None):
                self.putFields(value)
                if not self.showQObjectNames:
                    metaObjectPtr = self.extractMetaObjectPtr(value.address(), value.type)
                if metaObjectPtr:
                    self.putQObjectGuts(value, metaObjectPtr)


    def qtTypeInfoVersion(self):
        return 11 # FIXME

    def lookupType(self, typestring):
        return self.fromNativeType(self.lookupNativeType(typestring))

    def addToCache(self, typeobj):
        typename = typeobj.name
        if typename in self.typesReported:
            return
        self.typesReported[typename] = True
        self.typesToReport[typename] = typeobj

    class Value:
        def __init__(self, dumper):
            self.dumper = dumper
            self.name = None
            self.type = None
            self.ldata = None
            self.laddress = None
            self.nativeValue = None
            self.lIsInScope = True

        def check(self):
            if self.laddress is not None and not self.dumper.isInt(self.laddress):
                error("INCONSISTENT ADDRESS: %s" % type(self.laddress))
            if self.type is not None and not isinstance(self.type, self.dumper.Type):
                error("INCONSISTENT TYPE: %s" % type(self.type))

        def __str__(self):
            #error("Not implemented")
            return self.stringify()

        def stringify(self):
            self.check()
            addr = "None" if self.laddress is None else ("0x%x" % self.laddress)
            return "Value(name='%s',type=%s,data=%s,address=%s,nativeValue=%s)" \
                    % (self.name, self.type.stringify(), self.dumper.hexencode(self.ldata),
                        addr, self.nativeValue)

        def display(self):
            if self.type.code == TypeCodeEnum:
                return self.type.enumDisplay(self.extractInteger(self.type.bitsize(), False))
            simple = self.value()
            if simple is not None:
                return str(simple)
            if self.type.code == TypeCodeComplex:
                if self.nativeValue is not None:
                    if self.dumper.isLldb:
                        return str(self.nativeValue.GetValue())
                    else:
                        return str( self.nativeValue)
            if self.nativeValue is not None:
                return str(self.nativeValue)
                #return "Value(nativeValue=%s)" % self.nativeValue
            if self.ldata is not None:
                if sys.version_info[0] == 2 and isinstance(self.ldata, buffer):
                    return bytes(self.data).encode("hex")
                return self.data.encode("hex")
            if self.laddress is not None:
                return "value of type %s at address 0x%x" % (self.type, self.laddress)
            return "<unknown data>"

        def simpleDisplay(self, showAddress=True):
            res = self.value()
            if res is None:
                res = ''
            else:
                res = str(res)
            if showAddress and self.laddress:
                if len(res):
                    res += ' '
                res += "@0x%x" % self.laddress
            return res

        def integer(self):
            unsigned = self.type.stripTypedefs().name.startswith("unsigned")
            bitsize = self.type.bitsize()
            return self.extractInteger(bitsize, unsigned)

        def floatingPoint(self):
            if self.type.size() == 8:
                return self.extractSomething('d', 64)
            if self.type.size() == 4:
                return self.extractSomething('f', 32)
            error("BAD FLOAT DATA: %s SIZE: %s" % (self, self.type.size()))

        def pointer(self):
            return self.extractInteger(8 * self.dumper.ptrSize(), True)

        def value(self):
            if self.type is not None:
                if self.type.code == TypeCodeIntegral:
                    return self.integer()
                if self.type.code == TypeCodeFloat:
                    return self.floatingPoint()
                if self.type.code == TypeCodeTypedef:
                    return self.cast(self.type.stripTypedefs()).value()
                if self.type.stripTypedefs().code == TypeCodePointer:
                    return self.pointer()
            return None

        def extractPointer(self):
            return self.split('p')[0]

        def __getitem__(self, index):
            #warn("GET ITEM %s %s" % (self, index))
            self.check()
            if self.type.code == TypeCodeTypedef:
                #warn("GET ITEM %s STRIP TYPEDEFS TO %s" % (self, self.type.stripTypedefs()))
                return self.cast(self.type.stripTypedefs()).__getitem__(index)
            if isinstance(index, str):
                if self.type.code == TypeCodePointer:
                    #warn("GET ITEM %s DEREFERENCE TO %s" % (self, self.dereference()))
                    return self.dereference().__getitem__(index)
                field = self.dumper.Field(self.dumper)
                field.parentType = self.type
                field.name = index
            elif isinstance(index, self.dumper.Field):
                field = index
            elif self.dumper.isInt(index):
                return self.members()[index]
            else:
                error("BAD INDEX TYPE %s" % type(index))
            field.check()

            #warn("EXTRACT FIELD: %s, BASE 0x%x" % (field, self.address()))
            if self.type.code == TypeCodePointer:
                #warn("IS TYPEDEFED POINTER!")
                res = self.dereference()
                #warn("WAS POINTER: %s" % res)
                return res.extractField(field)

            return self.extractField(field)

        def extractField(self, field):
            #warn("PARENT BASE 0x%x" % self.address())
            if self.type.code == TypeCodeTypedef:
                error("WRONG")
            if not isinstance(field, self.dumper.Field):
                error("BAD INDEX TYPE %s" % type(field))

            val = None
            if self.nativeValue is not None:
                #warn("NATIVE, FIELD TYPE: %s " % field)
                val = self.dumper.nativeValueChildFromField(self.nativeValue, field)
                #warn("BAD INDEX XX VAL: %s TYPE: %s INDEX TYPE: %s "
                #    % (self, self.type, type(field)))

            #warn("FIELD: %s " % field)
            fieldType = field.fieldType()
            fieldBitsize = field.bitsize()
            fieldSize = None if fieldBitsize is None else fieldBitsize >> 3
            #warn("BITPOS %s BITSIZE: %s" % (fieldBitpos, fieldBitsize))

            if val is None:
                val = self.dumper.Value(self.dumper)
                val.type = fieldType
                val.name = field.name

                if self.laddress is not None:
                    #warn("ADDRESS")
                    fieldBitpos = field.bitpos()
                    fieldOffset = None if fieldBitpos is None else fieldBitpos >> 3
                    if  fieldBitpos is not None:
                        #warn("BITPOS: %s" % fieldBitpos)
                        val.laddress = self.laddress + fieldOffset
                    else:
                        error("NO IDEA 1")
                elif len(self.ldata) > 0:
                    #warn("DATA")
                    fieldBitpos = field.bitpos()
                    fieldOffset = None if fieldBitpos is None else fieldBitpos >> 3
                    if fieldBitpos is not None:
                        val.ldata = self.ldata[fieldOffset:fieldOffset + fieldSize]
                    else:
                        error("NO IDEA 2")
                else:
                    error("NO IDEA 3")

            #warn("BITPOS %s BITSIZE: %s" % (fieldBitpos, fieldBitsize))
            if fieldBitsize is not None and fieldBitsize % 8 != 0:
                fieldBitpos = field.bitpos()
                #warn("CORRECTING: FITPOS %s BITSIZE: %s" % (fieldBitpos, fieldBitsize))
                typeobj = fieldType
                typeobj.lbitsize = fieldBitsize
                data = val.extractInteger(fieldBitsize, True)
                data = data >> (fieldBitpos & 3)
                data = data & ((1 << fieldBitsize) - 1)
                val.laddress = None
                val.ldata = bytes(struct.pack('Q', data))
                val.type = typeobj

            #warn("GOT VAL %s FOR FIELD %s" % (val, field))
            val.check()
            val.lbitsize = field.bitsize()
            return val

        def members(self):
            members = []
            for field in self.type.fields():
                if not field.isBaseClass:
                    members.append(self.extractField(field))
            return members

        def __add__(self, other):
            self.check()
            if self.dumper.isInt(other):
                #warn("OTHER INT: %s" % other)
                if self.nativeValue is not None:
                    #warn("OTHER NATIVE: %s" % self.nativeValue)
                    #warn("OTHER RESULT 1: %s" % (self.nativeValue + other))
                    res = self.dumper.fromNativeValue(self.nativeValue + other)
                    #warn("OTHER RESULT 2: %s" % (self.nativeValue + other))
                    #warn("OTHER COOKED: 0x%x" % res.pointer())
                    #warn("OTHER COOKED X: 0x%x" % res.nativeValue)
                    return res
            error("BAD DATA TO ADD TO: %s %s" % (self.type, other))

        def dereference(self):
            self.check()
            if self.nativeValue is not None:
                res = self.dumper.nativeValueDereference(self.nativeValue)
                if res is not None:
                    return res
            if self.laddress is not None:
                val = self.dumper.Value(self.dumper)
                val.type = self.type.dereference()
                #val.ldata = bytes(self.dumper.readRawMemory(self.laddress, val.type.size()))
                #val.laddress = self.dumper.extractPointer(self.laddress)
                #val.ldata = bytes(self.data(self.dumper.ptrSize()))
                #val.laddress = self.__int__()
                bitsize = self.dumper.ptrSize() * 8
                val.laddress = self.integer()
                #warn("DEREFERENCING %s AT 0x%x -- %s TO %s AT 0x%x --- %s" %
                #    (self.type, self.laddress, self.dumper.hexencode(self.data),
                #    val.type, val.laddress, self.dumper.hexencode(val.data)))
                return val
            error("BAD DATA TO DEREFERENCE: %s %s" % (self.type, type(self)))

        def extend(self, size):
            if self.type.size() < size:
                val = self.dumper.Value(self.dumper)
                val.laddress = None
                if sys.version_info[0] == 3:
                    val.ldata = self.ldata + bytes('\0' * (size - self.type.size()), encoding='latin1')
                else:
                    val.ldata = self.ldata + bytes('\0' * (size - self.type.size()))
                return val
            if self.type.size() == size:
                return self
            error("NOT IMPLEMENTED")

        def cast(self, typish):
            self.check()
            typeobj = self.dumper.createType(typish)
            if self.nativeValue is not None and typeobj.nativeType is not None:
                res = self.dumper.nativeValueCast(self.nativeValue, typeobj.nativeType)
                if res is not None:
                    return res
                #error("BAD NATIVE DATA TO CAST: %s %s" % (self.type, typeobj))
            val = self.dumper.Value(self.dumper)
            val.laddress = self.laddress
            val.ldata = self.ldata
            val.type = typeobj
            #warn("CAST %s %s" % (self.type.stringify(), typeobj.stringify()))
            return val

        def downcast(self):
            self.check()
            if self.nativeValue is not None:
                return self.dumper.nativeValueDownCast(self.nativeValue)
            return self

        def isInScope(self):
            return self.lIsInScope

        def address(self):
            self.check()
            return self.laddress

        def data(self, size = None):
            self.check()
            if self.ldata is not None:
                if len(self.ldata) > 0:
                    if size is None:
                        return self.ldata
                    if size == len(self.ldata):
                        return self.ldata
                    if size < len(self.ldata):
                        return self.ldata[:size]
                    error("DATA PRESENT, BUT NOT BIG ENOUGH: %s WANT: %s"
                        % (self.stringify(), size))
            if self.laddress is not None:
                if size is None:
                    size = self.type.size()
                res = self.dumper.readRawMemory(self.laddress, size)
                if len(res) > 0:
                    return res
            if self.nativeValue is not None:
                if size is None:
                    size = self.type.size()
                res = self.dumper.nativeValueAsBytes(self.nativeValue, size)
                if len(res) > 0:
                    return res
                return res
            error("CANNOT CONVERT TO BYTES: %s" % self)

        def extractInteger(self, bitsize, unsigned):
            self.check()
            size = (bitsize + 7) >> 3
            if size == 8:
                code = "Q" if unsigned else "q"
            elif size == 4:
                code = "I" if unsigned else "i"
            elif size == 2:
                code = "H" if unsigned else "h"
            elif size == 1:
                code = "B" if unsigned else "b"
            else:
                code = None
            if code is None:
                return None
            rawBytes = self.data(size)
            try:
                return struct.unpack_from(code, rawBytes, 0)[0]
            except:
                pass
            error("Cannot extract: Code: %s Bytes: %s Bitsize: %s Size: %s"
                % (code, self.dumper.hexencode(rawBytes), bitsize, size))

        def extractSomething(self, code, bitsize):
            self.check()
            size = (bitsize + 7) >> 3
            rawBytes = self.data(size)
            return struct.unpack_from(code, rawBytes, 0)[0]

        def to(self, pattern):
            return self.split(pattern)[0]

        def split(self, pattern):
            #warn("EXTRACT STRUCT FROM: %s" % self.type)
            (pp, size, fields) = self.dumper.describeStruct(pattern)
            #warn("SIZE: %s " % size)
            result = struct.unpack_from(pp, self.data(size))
            def structFixer(field, thing):
                #warn("STRUCT MEMBER: %s" % type(thing))
                if field.isStruct:
                    if field.ltype != field.fieldType():
                        error("DO NOT SIMPLIFY")
                    #warn("FIELD POS: %s" % field.ltype)
                    #warn("FIELD TYE: %s" % field.fieldType())
                    res = self.dumper.createValue(thing, field.fieldType())
                    #warn("RES TYPE: %s" % res.type)
                    if self.laddress is not None:
                        res.laddress = self.laddress + field.offset()
                    return res
                return thing
            if len(fields) != len(result):
                error("STRUCT ERROR: %s %s" (fields, result))
            return tuple(map(structFixer, fields, result))

    def checkPointer(self, p, align = 1):
        ptr = p if self.isInt(p) else p.pointer()
        self.readRawMemory(ptr, 1)

    class Type:
        def __init__(self, dumper):
            self.dumper = dumper
            self.name = None
            self.nativeType = None
            self.lfields = None
            self.lbitsize = None
            self.lbitpos = None
            self.templateArguments = None
            self.code = None

        def __str__(self):
            self.check()
            error("Not implemented")
            return self.name
            #error("Not implemented")

        def stringify(self):
            return "Type(name='%s',bsize=%s,bpos=%s,code=%s,native=%s)" \
                    % (self.name, self.lbitsize, self.lbitpos, self.code, self.nativeType is not None)

        def __getitem__(self, index):
            if self.dumper.isInt(index):
                return self.templateArgument(index)
            error("CANNOT INDEX TYPE")

        def check(self):
            if self.name is None:
                error("TYPE WITHOUT NAME")

        def dereference(self):
            self.check()
            if self.nativeType is not None:
                return self.dumper.nativeTypeDereference(self.nativeType)
            error("DONT KNOW HOW TO DEREF: %s" % self.name)

        def unqualified(self):
            if self.nativeType is not None:
                return self.dumper.nativeTypeUnqualified(self.nativeType)
            return self

        def templateArgument(self, position, numeric = False):
            if self.templateArguments is not None:
                return self.templateArguments[position]
            nativeType = self.nativeType
            #warn("NATIVE TYPE 0: %s" % dir(nativeType))
            if nativeType is None:
                nativeType = self.dumper.lookupNativeType(self.name)
                #warn("NATIVE TYPE 1: %s" % dir(nativeType))
            if nativeType is not None:
                return self.dumper.nativeTypeTemplateArgument(nativeType, position, numeric)
            res = self.dumper.extractTemplateArgument(self.name, position)
            #warn("TEMPLATE ARG: RES: %s" % res)
            if numeric:
                return int(res)
            return self.dumper.createType(res)

        def simpleEncoding(self):
            res = {
                'bool' : 'int:1',
                'char' : 'int:1',
                'signed char' : 'int:1',
                'unsigned char' : 'uint:1',
                'short' : 'int:2',
                'unsigned short' : 'uint:2',
                'int' : 'int:4',
                'unsigned int' : 'uint:4',
                'long long' : 'int:8',
                'unsigned long long' : 'uint:8',
                'float': 'float:4',
                'double': 'float:8'
            }.get(self.name, None)
            return res

        def isSimpleType(self):
            return self.code in (TypeCodeIntegral, TypeCodeFloat, TypeCodeEnum)

        def alignment(self):
            if self.isSimpleType():
                if self.name == 'double':
                    return self.dumper.ptrSize() # Crude approximation.
                return self.size()
            if self.code == TypeCodePointer:
                return self.dumper.ptrSize()
            fields = self.fields()
            align = 1
            for field in fields:
                a = field.fieldType().alignment()
                #warn("  SUBFIELD: %s ALIGN: %s" % (field.name, a))
                if a is not None and a > align:
                    align = a
            #warn("COMPUTED ALIGNMENT: %s " % align)
            return align

        def pointer(self):
            if self.nativeType is not None:
                return self.dumper.nativeTypePointer(self.nativeType)
            error("Cannot create pointer type for %s" % self)

        def splitArrayType(self):
            # -> (inner type, count)
            if not self.code == TypeCodeArray:
                error("Not an array")
            s = self.name
            pos1 = s.rfind('[')
            pos2 = s.find(']', pos1)
            itemCount = s[pos1+1:pos2]
            return (self.dumper.createType(s[0:pos1].strip()), int(s[pos1+1:pos2]))

        def target(self):
            if self.nativeType is not None:
                target = self.dumper.nativeTypeTarget(self.nativeType)
                #warn("DEREFERENCING: %s -> %s " % (self.nativeType, target))
                if target is not None:
                    return target
            if self.code == TypeCodeArray:
                (innerType, itemCount) = self.splitArrayType()
                #warn("EXTRACTING ARRAY TYPE: %s -> %s" % (self, innerType))
                # HACK for LLDB 320:
                if innerType.code is None and innerType.name.endswith(']'):
                    innerType.code = TypeCodeArray
                return innerType

            strippedType = self.stripTypdefs()
            if strippedType.name != self.name:
                return strippedType.target()
            error("DONT KNOW TARGET FOR: %s" % self)

        def fields(self):
            #warn("GETTING FIELDS FOR: %s" % self.name)
            if self.lfields is not None:
                warn("USING LFIELDS: %s" % self.lfields)
                return self.lfields
            nativeType = self.nativeType
            if nativeType is None:
                nativeType = self.dumper.lookupNativeType(self.name)
                #warn("FIELDS LOOKING UP NATIVE TYPE FOR %s -> %s" % (self.name, nativeType))
            if nativeType is not None:
                #warn("FIELDS USING NATIVE TYPE %s" % nativeType)
                fields = self.dumper.nativeTypeFields(nativeType)
                #warn("FIELDS RES: %s FOR %s" % (fields, nativeType))
                return fields
            error("DONT KNOW FIELDS FOR: %s" % self)
            return []

        def firstBase(self):
            if self.nativeType is not None:
                return self.dumper.nativeTypeFirstBase(self.nativeType)
            return None

        def field(self, name, bitoffset = 0):
            #warn("GETTING FIELD %s FOR: %s" % (name, self.name))
            for f in self.fields():
                #warn("EXAMINING MEMBER %s" % f.name)
                if f.name == name:
                    ff = copy.copy(f)
                    if ff.lbitpos is None:
                        ff.lbitpos = bitoffset
                    else:
                        ff.lbitpos += bitoffset
                    #warn("FOUND: %s" % ff)
                    return ff
                if f.isBaseClass:
                    #warn("EXAMINING BASE %s" % f.ltype)
                    res = f.ltype.field(name, bitoffset + f.bitpos())
                    if res is not None:
                        return res
            #warn("FIELD %s NOT FOUND IN %s" % (name, self))
            return None

        def stripTypedefs(self):
            if self.code != TypeCodeTypedef:
                #warn("NO TYPEDEF: %s" % self)
                return self
            if self.nativeType is not None:
                res = self.dumper.nativeTypeStripTypedefs(self.nativeType)
                #warn("STRIP TYPEDEF: %s -> %s" % (self, res))
                return res
            error("DONT KNOW HOW TO STRIP TYPEDEFS FROM %s" % s)

        def size(self):
            bs = self.bitsize()
            if bs % 8 != 0:
                warn("ODD SIZE: %s" % self)
            return (7 + bs) >> 3

        def bitsize(self):
            if self.lbitsize is not None:
                return self.lbitsize
            if self.code == TypeCodeArray:
                (innerType, itemCount) = self.splitArrayType()
                return itemCount * innerType.bitsize()
            error("DONT KNOW SIZE: %s" % self.name)

        def isMovableType(self):
            if self.code in (TypeCodePointer, TypeCodeIntegral, TypeCodeFloat):
                return True
            strippedName = self.dumper.stripNamespaceFromType(self.name)
            if strippedName in (
                    "QBrush", "QBitArray", "QByteArray", "QCustomTypeInfo",
                    "QChar", "QDate", "QDateTime", "QFileInfo", "QFixed",
                    "QFixedPoint", "QFixedSize", "QHashDummyValue", "QIcon",
                    "QImage", "QLine", "QLineF", "QLatin1Char", "QLocale",
                    "QMatrix", "QModelIndex", "QPoint", "QPointF", "QPen",
                    "QPersistentModelIndex", "QResourceRoot", "QRect", "QRectF",
                    "QRegExp", "QSize", "QSizeF", "QString", "QTime", "QTextBlock",
                    "QUrl", "QVariant",
                    "QXmlStreamAttribute", "QXmlStreamNamespaceDeclaration",
                    "QXmlStreamNotationDeclaration", "QXmlStreamEntityDeclaration"
                    ):
                return True
            return strippedName == "QStringList" and self.dumper.qtVersion() >= 0x050000

        def enumDisplay(self, intval):
            if self.nativeType is not None:
                return self.dumper.nativeTypeEnumDisplay(self.nativeType, intval)
            return "%d" % intval

    class Field:
        def __init__(self, dumper):
            self.dumper = dumper
            self.name = None
            self.baseIndex = None    # Base class index if parent is structure
            self.nativeIndex = None   # Backend-defined index value
            self.isBaseClass = False
            self.isVirtualBase = False
            self.ltype = None
            self.parentType = None
            self.lbitsize = None
            self.lbitpos = None
            self.isStruct = False

        def __str__(self):
            return ("Field(name='%s',ltype=%s,parentType=%s,bpos=%s,bsize=%s,"
                     + "bidx=%s,nidx=%s)") \
                    % (self.name, self.ltype, self.parentType,
                       self.lbitpos, self.lbitsize,
                       self.baseIndex, self.nativeIndex)

        def check(self):
            if self.parentType.code == TypeCodePointer:
                error("POINTER NOT POSSIBLE AS FIELD PARENT")
            if self.parentType.code == TypeCodeTypedef:
                error("TYPEDEFS NOT ALLOWED AS FIELD PARENT")

        def size(self):
            return self.bitsize() >> 3

        def offset(self):
            return self.bitpos() >> 3

        def bitsize(self):
            self.check()
            if self.lbitsize is not None:
                return self.lbitsize
            fieldType = self.fieldType()
            # FIXME: enforce return value != None.
            if fieldType is not None:
                return fieldType.bitsize()
            return None

        def bitpos(self):
            if self.lbitpos is not None:
                #warn("BITPOS KNOWN: %s %s" % (self.name, self.lbitpos))
                return self.lbitpos
            self.check()
            f = self.parentType.field(self.name)
            if f is not None:
                #warn("BITPOS FROM PARENT: %s" % self.parentType)
                return f.bitpos()
            error("DONT KNOW BITPOS FOR FIELD: %s " % self)

        def fieldType(self):
            if self.ltype is not None:
                return self.ltype
            if self.name is not None:
                field = self.parentType.field(self.name)
                if field is not None:
                    return field.fieldType()
            #error("CANT GET FIELD TYPE FOR %s" % self)
            return None

    def createType(self, typish, size = None):
        if isinstance(typish, self.Type):
            typish.check()
            return typish
        if isinstance(typish, str):
            if typish[0] == 'Q':
                if typish in ("QByteArray", "QString", "QList", "QStringList"):
                    typish = self.qtNamespace() + typish
                    size = self.ptrSize()
                elif typish == "QImage":
                    typish = self.qtNamespace() + typish
                    size = 2 * self.ptrSize()
                elif typish in ("QVariant", "QPointF", "QDateTime", "QRect"):
                    typish = self.qtNamespace() + typish
                    size = 16
                elif typish == "QPoint":
                    typish = self.qtNamespace() + typish
                    size = 8
                elif typish == "QChar":
                    typish = self.qtNamespace() + typish
                    size = 2
            elif typish in ("quint32", "qint32"):
                typish = self.qtNamespace() + typish
                size = 4

            #typeobj = self.Type(self)
            #typeobj.name = typish
            nativeType = self.lookupNativeType(typish) # FIXME: Remove?
            #warn("FOUND NAT TYPE: %s" % dir(nativeType))
            if nativeType is not None:
                #warn("USE FROM NATIVE")
                typeobj = self.fromNativeType(nativeType)
            else:
                #warn("FAKING")
                typeobj = self.Type(self)
                typeobj.name = typish
                if size is not None:
                    typeobj.lbitsize = 8 * size
            #warn("CREATE TYPE: %s" % typeobj)
            typeobj.check()
            return typeobj
        if self.isInt(typish):
            # Assume it is an typecode, create an "anonymous" Type
            typeobj = self.Type(self)
            typeobj.code = typish
            typeobj.lbitsize = 8 * size
            typeobj.name = ' '
            return typeobj
        error("NEED TYPE, NOT %s" % type(typish))

    def createValue(self, datish, typish):
        if self.isInt(datish):  # Used as address.
            #warn("CREATING %s AT 0x%x" % (typish, address))
            val = self.Value(self)
            val.laddress = datish
            val.type = self.createType(typish)
            val.check()
            return val
        if isinstance(datish, bytes):
            #warn("CREATING %s WITH DATA %s" % (typish, self.hexencode(datish)))
            val = self.Value(self)
            val.ldata = datish
            val.type = self.createType(typish)
            val.type.lbitsize = 8 * len(datish)
            val.check()
            return val
        error("EXPECTING ADDRESS OR BYTES, GOT %s" % type(datish))

    def createListItem(self, data, innerTypish):
        innerType = self.createType(innerTypish)
        typeobj = self.Type(self)
        typeobj.name = self.qtNamespace() + "QList<%s>" % innerType.name
        typeobj.templateArguments = [innerType]
        typeobj.lbitsize = 8 * self.ptrSize()
        val = self.Value(self)
        val.ldata = data
        val.type = typeobj
        return val

    def createVectorItem(self, data, innerTypish):
        innerType = self.createType(innerTypish)
        typeobj = self.Type(self)
        typeobj.name = self.qtNamespace() + "QVector<%s>" % innerType.name
        typeobj.templateArguments = [innerType]
        typeobj.lbitsize = 8 * self.ptrSize()
        val = self.Value(self)
        val.ldata = data
        val.type = typeobj
        return val

    class StructBuilder:
        def __init__(self, dumper):
            self.dumper = dumper
            self.pattern = ""
            self.currentBitsize = 0
            self.fields = []
            self.autoPadNext = False

        def fieldAlignment(self, fieldSize, fieldType):
            if fieldType is not None:
                align = self.dumper.createType(fieldType).alignment()
                #warn("COMPUTED ALIGNMENT FOR %s: %s" % (fieldType, align))
                if align is not None:
                    return align
            if fieldSize <= 8:
                align = (0, 1, 2, 4, 4, 8, 8, 8, 8)[fieldSize]
                #warn("GUESSED ALIGNMENT FROM SIZE: %s" % align)
                return align
            #warn("GUESSED ALIGNMENT: %s" % 8)
            return 8

        def addField(self, fieldSize, fieldCode = None, fieldIsStruct = False,
                     fieldName = None, fieldType = None):

            if fieldType is not None:
                fieldType = self.dumper.createType(fieldType)
            if fieldSize is None and fieldType is not None:
                fieldSize = fieldType.size()
            if fieldCode is None:
                fieldCode = "%ss" % fieldSize

            if self.autoPadNext:
                align = self.fieldAlignment(fieldSize, fieldType)
                self.currentBitsize = 8 * ((self.currentBitsize + 7) >> 3)  # Fill up byte.
                padding = (align - (self.currentBitsize >> 3)) % align
                #warn("AUTO PADDING AT %s BITS BY %s BYTES" % (self.currentBitsize, padding))
                field = self.dumper.Field(self.dumper)
                field.code = None
                #field.lbitpos = self.currentBitsize
                #field.lbitsize = padding * 8
                self.pattern += "%ds" % padding
                self.currentBitsize += padding * 8
                self.fields.append(field)
                self.autoPadNext = False

            field = self.dumper.Field(self.dumper)
            field.name = fieldName
            field.ltype = fieldType
            field.code = fieldCode
            field.isStruct = fieldIsStruct
            field.lbitpos = self.currentBitsize
            field.lbitsize = fieldSize * 8

            self.pattern += fieldCode
            self.currentBitsize += fieldSize * 8
            self.fields.append(field)

    def describeStruct(self, pattern):
        if pattern in self.structPatternCache:
            return self.structPatternCache[pattern]
        ptrSize = self.ptrSize()
        builder = self.StructBuilder(self)
        n = None
        typeName = ""
        readingTypeName = False
        for c in pattern:
            if readingTypeName:
                if c == '}':
                    readingTypeName = False
                    builder.addField(n, fieldIsStruct = True, fieldType = typeName)
                    typeName = None
                    n = None
                else:
                    typeName += c
            elif c == 'p': # Pointer as int
                builder.addField(ptrSize, 'Q' if ptrSize == 8 else 'I')
            elif c == 'P': # Pointer as Value
                builder.addField(ptrSize, '%ss' % ptrSize)
            elif c in ('q', 'Q', 'd'):
                builder.addField(8, c)
            elif c in ('i', 'I', 'f'):
                builder.addField(4, c)
            elif c in ('h', 'H'):
                builder.addField(2, c)
            elif c in ('b', 'B', 'c'):
                builder.addField(1, c)
            elif c >= '0' and c <= '9':
                if n is None:
                    n = ""
                n += c
            elif c == 's':
                builder.addField(int(n))
                n = None
            elif c == '{':
                readingTypeName = True
                typeName = ""
            elif c == '@':  # Automatic padding.
                builder.autoPadNext = True
            else:
                error("UNKNOWN STRUCT CODE: %s" % c)
        pp = builder.pattern
        size = (builder.currentBitsize + 7) >> 3  # FIXME: Tail padding missing.
        fields = builder.fields
        self.structPatternCache[pattern] = (pp, size, fields)
        #warn("PP: %s -> %s %s %s" % (pattern, pp, size, fields))
        return (pp, size, fields)

import py
from pypy.translator.cli.test.runtest import CliTest
from pypy.translator.oosupport.test_template.class_ import BaseTestClass, BaseTestSpecialcase

# ====> ../../oosupport/test_template/class_.py

class TestCliClass(CliTest, BaseTestClass):    
    pass

class TestCliSpecialCase(CliTest, BaseTestSpecialcase):
    pass

# 
# Copyright 2016-present Ciena Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#!/usr/bin/env python
##Generate a port map for 100 subscribers based on veth pairs
import sys
header = '''###This file is auto-generated. Do not EDIT###'''
def generate_port_map(num = 100):
    print("g_subscriber_port_map = {}")
    for i in xrange(1, num+1):
        intf = 'veth' + str(2*i-2)
        print("g_subscriber_port_map[%d]='%s'" %(i, intf))
        print("g_subscriber_port_map['%s']=%d" %(intf, i))

if __name__ == '__main__':
    num = 100
    if len(sys.argv) > 1:
        num = int(sys.argv[1])
    print(header)
    generate_port_map(num)

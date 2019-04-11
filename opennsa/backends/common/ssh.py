'''
Created on Jun 6, 2016

@author: sally
'''


import os
import sys
#from twisted.internet import defer

try:
    import paramiko
except ImportError:
    raise ImportError('Importing paramiko SSH module or its dependencies failed.')
    sys.exit(1)


# things to do
# check fingerprint -> verify Host Key


class SSHConnection: #(host, port, fingerprint, username, public_key, private_key):


    def __init__(self, host, port, username, public_key_path=None, private_key_path=None, password = None):

        self.host = host
        self.port = port
        self.username = username
        self.public_key_path = public_key_path
        self.private_key_path = private_key_path
        self.password = password



    def startConnection(self):

        client = paramiko.SSHClient()
        paramiko.util.log_to_file('SSH_session.log')
        client.load_system_host_keys()
#       pathtotake = None

#       if self.public_key_path:
#           if os.path.exists(self.public_key_path):
#               pathtotake = self.public_key_path
#           elif os.path.exists(os.path.expanduser(self.public_key_path)):
#               pathtotake = os.path.expanduser(self.public_key_path)
#
                    # can comment out elif if it is known that public_key_path is straightforward
                    # and does not need to be expanded . . .
#       else:
#           pathtotake = os.path.expanduser('~/.ssh/known_hosts')

#       client.load_host_keys(pathtotake)
        client.set_missing_host_key_policy(paramiko.WarningPolicy())

#       hostkey = None
#       hostkeytype = None

        privkey=None

        if self.private_key_path: #self.public_key_path

            if os.path.exists(self.private_key_path):# and os.path.exists(self.public_key_path):

                privkey = paramiko.RSAKey.from_private_key_file(self.private_key_path)
#                try:
#                    host_keys = paramiko.util.load_host_keys(self.public_key_path)
#                    if self.host in host_keys:
#                            hostkeytype = host_keys[self.host].keys()[0]
#                            hostkey = host_keys[self.host][hostkeytype]
#                except IOError:
#                   print('Unable to open host keys file')
#                    host_keys = {}

            elif os.path.exists(os.path.expanduser(self.private_key_path)):

                privkey = paramiko.RSAKey.from_private_key_file(os.path.expanduser(self.private_key_path))

            else:
                raise TypeError("Incorrect private key path or file does not exist")
                sys.exit(1)



        try:
            if privkey:
                client.connect(hostname=self.host, username=self.username,pkey=privkey)

            elif self.password:
            #go with username/pass
                client.connect(hostname = self.host, username=self.username, password=self.password)
            else:

                raise AssertionError('No keys or password supplied')
                sys.exit(1)

        except paramiko.AuthenticationException:

            print ('Connection Failed')
            raise paramiko.AuthenticationException()
            #exit

        return client

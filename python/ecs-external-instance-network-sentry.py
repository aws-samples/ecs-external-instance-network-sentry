import argparse
import logging
import textwrap
import time
import docker
import sys
import socket
import ssl

# configure environment:
#   - external variables..
ap = argparse.ArgumentParser(
     prog="ecs-external-instance-network-sentry",
     formatter_class=argparse.RawDescriptionHelpFormatter,
     description=textwrap.dedent('''\
     Purpose:
     --------------
     For use on ECS Anywhere external hosts:
     Configures ECS orchestrated containers to automatically restart
     on failure when on-region ecs control-plane is detected to be unreachable.'''))
ap.add_argument("-r", "--region", required=True,
   help="AWS region where ecs cluster is located.")
ap.add_argument("-i", "--interval", default=20, required=False,
   help="Interval in seconds sentry will sleep between connectivity checks.")
ap.add_argument("-n", "--retries", default=0, required=False,
   help="Number of times Docker will restart a crashing container.")
ap.add_argument("-l", "--logfile", default="/tmp/ecs-external-instance-network-sentry.log", required=False,
   help="Logfile name & location.")
ap.add_argument("-k", "--loglevel", default="DEBUG", required=False,
   help="Log data event severity.")
args = vars(ap.parse_args())
#   - internal variables..
client = docker.from_env()
context = ssl.create_default_context()
ecs_host = ("ecs." + str(args["region"]) + ".amazonaws.com")
ecs_request_data = "GET / HTTP/1.1\r\nHost: " + ecs_host + "\r\nAccept: text/html\r\n\r\n"
port = 443
all_data=[]

# logging:
#   - configure logging..
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    handlers=[
        RotatingFileHandler(
          args["logfile"],
          maxBytes=5242880,
          backupCount=5
        )
    ],
    level=args["loglevel"],
    format='%(asctime)s %(levelname)s PID_%(process)d %(message)s'
)
#   - commence logging..
logging.info("[startup] ecs-external-instance-network-sentry - starting..")
logging.info("[startup] arg - aws region: " + str(args["region"]))
logging.info("[startup] arg - interval: " + str(args["interval"]))
logging.info("[startup] arg - retries: " + str(args["retries"]))
logging.info("[startup] arg - logfile: " + str(args["logfile"]))
logging.info("[startup] arg - loglevel: logging." + str(args["loglevel"]))

# main logic as infinite loop..
while True:
    # test connectivity to ecs on-region..
    logging.info("[begin] connectivity test..")
    socket_err = 0
    logging.info("[connect] connecting to ecs at " + str(args["region"]) + "..")
    
    logging.info("[connect] create network socket..")
    try:
        sock = socket.create_connection((ecs_host, port))
    except socket.error as e:
        logging.error("[connect] error creating network socket: %s" % e)
        socket_err = 2

    logging.info("[connect] connecting to host..")
    if socket_err != 2:
        try:
            ssock = context.wrap_socket(sock, server_hostname=ecs_host)
            ssock.settimeout(10)
        except socket.gaierror as e:
            logging.error("[connect] name/address error connecting to host: %s" % e)
            socket_err = 1
        except socket.error as e:
            logging.error("[connect] error connecting to host: %s" % e)
            socket_err = 1

        logging.info("[connect] send/receive data..")
        if socket_err == 0:
            all_data = []
            try:
                ssock.send(ecs_request_data.encode())
                while True:
                    data = ssock.recv(1024)
                    all_data.append(data)
                    if not data:
                        break
                logging.debug("[connect] data: " + str(all_data))
            except socket.error as e:
                logging.error("[connect] error send/receive data: %s" % e)
                socket_err = 1

        if socket_err == 0:
            logging.info("[connect] ecs at " + str(args["region"]) + " is available..")

    # docker configuration:
    #   - error connecting to ecs on-region: update restart policy for ecs managed containers..
    if socket_err != 0:

        logging.info("[ecs-offline] ecs unreachable, configuring container restart policy..")
        for container in client.containers.list():

            # pause the ecs agent..
            if container.name == "ecs-agent":
                if (container.attrs["State"]["Status"]) != "paused":
                    container.pause()
                    logging.info("[ecs-offline] ecs agent paused..")

            # update restart policy for ecs managed containers..
            if container.name != "ecs-agent":
                if "com.amazonaws.ecs.cluster" in container.labels:
                    if (container.attrs["HostConfig"]["RestartPolicy"]["Name"]) != "on-failure":
                        container.update(restart_policy={"Name": "on-failure", "MaximumRetryCount": int(args["retries"])})
                        container.reload()
                        logging.info("[ecs-offline] container name: " + str(container.name))                        
                        logging.info("[ecs-offline] ecs cluster: " + str(container.labels["com.amazonaws.ecs.cluster"]))
                        logging.info("[ecs-offline] set container restart policy: " + str(container.attrs["HostConfig"]["RestartPolicy"]))

    #   - no error connecting to ecs on-region: clean-up if post network outage..
    else:

        logging.info("[ecs-online] ecs is reachable..")
        for container in client.containers.list():
            
            if container.name != "ecs-agent":
                if "com.amazonaws.ecs.cluster" in container.labels:
                    
                    # update ecs managed containers:
                    #   - stop & remove containers that have restarted..
                    if (container.attrs["HostConfig"]["RestartPolicy"]["Name"]) == "on-failure":
                        if container.attrs["RestartCount"] > 0:
                            logging.info("[ecs-online] container name: " + str(container.name))                        
                            logging.info("[ecs-online] ecs cluster: " + str(container.labels["com.amazonaws.ecs.cluster"]))
                            logging.info("[ecs-online] container has been restarted by docker, stopping & removing..")
                            container.stop()
                            container.remove()
                    #   - update restart policy for containers that have not restarted..
                        else:
                            container.update(restart_policy={"Name": "no"})
                            container.reload()
                            logging.info("[ecs-online] container name: " + str(container.name))
                            logging.info("[ecs-online] ecs cluster: " + str(container.labels["com.amazonaws.ecs.cluster"]))
                            logging.info("[ecs-online] set container restart policy: " + str(container.attrs["HostConfig"]["RestartPolicy"]))

            # unpause the ecs agent..
            if container.name == "ecs-agent":
                if (container.attrs["State"]["Status"]) == "paused":
                    container.unpause()
                    logging.info("[ecs-online] ecs agent unpaused..")

    logging.info("[end] sleeping for " + str(args["interval"]) + " seconds..")
    time.sleep(int(args["interval"]))

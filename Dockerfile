FROM quay.io/frrouting/frr:10.7.0@sha256:65e5967b922572c0565d968388fb06af69d7e9b3b3eea40ad7e3810687667f68 AS router
RUN apk add --no-cache tcpdump iproute2 iputils traceroute && touch /etc/frr/vtysh.conf

FROM alpine:3.22.5@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce AS host
RUN apk add --no-cache iproute2 iputils traceroute

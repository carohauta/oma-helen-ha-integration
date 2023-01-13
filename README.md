# Oma Helen Home Assistant integration

[Home Assistant](https://www.home-assistant.io/) integration for [Oma Helen Python module](https://github.com/carohauta/oma-helen-cli). Periodically fetch your electricity consumption and estimated costs.

![Oma Helen integration example](example.png)

⚠️ **Note that the price estimation (and price fetching) currently works only for the [Helen Market Price Electricity](https://www.helen.fi/sahko/sahkosopimus/markkinahinta) contract type.** ⚠️

### How to install

Copy the `helen_energy/` folder into your HA `config/custom_components/` folder

#### Step-by-step manual installation

Login to your HA with SSH

1. Go to the HA configuration root folder, which is the same folder where your `configuration.yaml` is located. Run the following commands
```shell
cd custom_components # create this folder if it does not exists
git clone https://github.com/carohauta/oma-helen-ha-integration omahelen
mv omahelen/custom_components/helen_energy/ .
```
2. Add your Oma Helen credentials to the `secrets.yaml`
```yaml
oma_helen_username: <USERNAME>
oma_helen_password: <PASSWORD>
```
3. Add a new sensor with `helen_energy` platform to the `configuration.yaml`.
```yaml
sensor:
  - platform: helen_energy
    username: !secret oma_helen_username
    password: !secret oma_helen_password
```
4. Restart HA

[![CircleCI](https://img.shields.io/circleci/project/github/andersy005/dash-docsets/master.svg?style=for-the-badge&logo=circleci)](https://circleci.com/gh/andersy005/dash-docsets)

- [Dash Docsets](#dash-docsets)
  - [Docset Feeds](#docset-feeds)
  - [Zeal Issues](#zeal-issues)

# Dash Docsets

My Dash (https://kapeli.com/dash) docsets. Let the buyer beware ⚠️;)

**The main difference** between the docsets hosted in this repo and the official dash & dash user contributed docsets is that _these docsets are generated from the master branch of each project_.

Note: It's expected that these docsets should also work in [Zeal](https://zealdocs.org/).

![](./images/navigate.png)

## Docset Feeds

[Dash](https://kapeli.com/dash) and [Zeal](https://zealdocs.org/) can subscribe to the following feeds with a single click.

```bash
dash-feed://<URL encoded feed URL>
```

**⚠️ See [Zeal Issues](#zeal-issues)** for more information on how to fix them.

![](./images/how-to-add-feed.png)

- [dask](https://github.com/dask/dask): https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds/dask.xml
- [distributed](https://github.com/dask/distributed): https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds/distributed.xml
- [zarr](https://github.com/zarr-developers/zarr-python): https://raw.githubusercontent.com/andersy005/dash-docsets/docsets/feeds/zarr.xml

## Zeal Issues

When subscribing to docsets feeds, Zeal appears to be not working properly:

![](./images/zeal-failure.png)

You may get the following error:

![](./images/zeal-failure-diag.png)

**Solution**:

- Download docsets from https://github.com/andersy005/dash-docsets/tree/docsets/docsets
- Find Zeal's docset storage directory by navigating to `Edit` -> `Preferences` from Zeal Menu bar.

- Untar the downloaded docset into zeal's docset storage directory

  ```bash
  tar -zxvf docset.tgz --directory zeal-docset-storage-directory
  ```

  Replace `docset.tgz` with the location of the downloaded docset, and `zeal-docset-storage-directory` with the found zeal's docset storage directory

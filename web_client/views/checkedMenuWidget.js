import { wrap } from 'girder/utilities/PluginUtils';
import events from 'girder/events';
import { restRequest } from 'girder/rest';
import { handleClose } from 'girder/dialog';
import UploadWidget from 'girder/views/widgets/UploadWidget';

import CheckedMenuWidget from 'girder/views/widgets/CheckedMenuWidget';
import HierarchyWidget from 'girder/views/widgets/HierarchyWidget';

import CheckedMenuExtensionTemplate from '../templates/checkedMenuWidget.pug';

wrap(CheckedMenuWidget, 'render', function (render) {
    render.call(this);

    if (this.itemCount) {
        this.$el.find('.g-pick-checked').closest('li').after(
            CheckedMenuExtensionTemplate({
                folderCount: this.folderCount,
                itemCount: this.itemCount,
                pickedCount: this.pickedCount
            })
        );
    }
    return this;
});

wrap(HierarchyWidget, 'render', function (render) {
    render.call(this);
    if (!this.uploadAnnotation) {
        this.uploadAnnotation = () => {
            var cid = this.itemListView.checked[0],
                item = this.itemListView.collection.get(cid),
                itemId = item.id,
                parentView = this;
            new UploadWidget({
                el: $('#g-dialog-container'),
                title: 'Upload annotation',
                parent: item,
                parentType: 'item',
                parentView: this,
                overrideStart: true
            }).on('g:uploadStarted', function () {
                handleClose('upload');
                // it would be good to add progress feedback
                $('#g-dialog-container').modal('hide');
                var fr = new FileReader();
                fr.onload = (evt) => {
                    restRequest({
                        url: 'annotation/item/' + itemId,
                        method: 'POST',
                        contentType: 'application/json',
                        data: evt.target.result
                        // it would be good to add progress feedback
                    }).then(() => {
                        events.trigger('g:alert', {
                            icon: 'ok',
                            text: 'Annotation uploaded.',
                            type: 'success',
                            timeout: 4000
                        });
                        return null;
                    }).fail((err) => {
                        parentView.trigger('g:error', err);
                    });
                };
                // add an fr.onerror function here
                fr.readAsText(this.files[0]);
            }).render();
        };
        this.events['click a.g-upload-annotation'] = this.uploadAnnotation;
        this.delegateEvents();
    }
    return this;
});
